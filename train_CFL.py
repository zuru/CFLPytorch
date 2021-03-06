from CFLPytorch.StdConvsCFL import StdConvsCFL
from CFLPytorch.EquiConvsCFL import EquiConvsCFL
import argparse
import logging
#import sagemaker_containers
import os
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.parallel
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision
import torchvision.models
import torchvision.transforms as transforms
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader, Subset
from PIL import Image
import numpy as np
import pandas as pd
#from CFLPytorch.offsetcalculator import offcalc
import time
#import torchprof
from torch.utils.tensorboard import SummaryWriter
import mytransforms
from progressbar import progressbar
import itertools

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)



eps = 1e-10 #epsilon to improve numerical stability
  

def evaluate(pred, gt):
    """
    if map == 'edges':
        prediction_path_list = glob.glob(os.path.join(args.results,'EM_test')+'/*.jpg')
        gt_path_list = glob.glob(os.path.join(args.dataset, 'EM_gt')+'/*.jpg')
    if map == 'corners':
        prediction_path_list = glob.glob(os.path.join(args.results,'CM_test')+'/*.jpg')
        gt_path_list = glob.glob(os.path.join(args.dataset, 'CM_gt')+'/*.jpg')
    prediction_path_list.sort()
    gt_path_list.sort()
    """

    #P, R, Acc, f1, IoU = [], [], [], [], []
    # predicted image
    #prediction = Image.open(prediction_path_list[im])
    #pred_H, pred_W = pred.shape[0], pred.shape[1]
    #prediction = torch.tensor(prediction)/255.
    

    # gt image
    #gt = Image.open(gt_path_list[im])
    #gt = gt.resize([pred_W, pred_H])
    #gt = torch.tensor(gt)/255.
    gt = (gt.ge(0.1)).int()

    th=0.1
    gtpos=gt.eq(1)
    gtneg=gt.eq(0)
    predgt=pred.gt(th)
    predle=pred.le(th)
    tp = torch.sum((gtpos & predgt).float())
    tn = torch.sum((gtneg & predle).float())
    fp = torch.sum((gtneg & predgt).float())
    fn = torch.sum((gtpos & predle).float())

    # How accurate the positive predictions are
    #P.append(tp / (tp + fp))
    P = tp / (tp + fp)
    # Coverage of actual positive sample
    #R.append(tp / (tp + fn))
    R = (tp / (tp + fn))
    # Overall performance of model
    #Acc.append((tp + tn) / (tp + tn + fp + fn))
    Acc = ((tp + tn) / (tp + tn + fp + fn))
    # Hybrid metric useful for unbalanced classes 
    #f1.append(2 * (tp / (tp + fp))*(tp / (tp + fn))/((tp / (tp + fp))+(tp / (tp + fn))))
    f1 = (2 * (tp / (tp + fp))*(tp / (tp + fn))/((tp / (tp + fp))+(tp / (tp + fn))))
    # Intersection over Union
    #IoU.append(tp / (tp + fp + fn))
    IoU = (tp / (tp + fp + fn))
      

    return torch.mean(P), torch.mean(R), torch.mean(Acc), torch.mean(f1), torch.mean(IoU)
    #return P, R, Acc, f1, IoU


def ce_loss(pred, gt):
    '''
    pred and gt have to be the same dimensions of N x C x H x W
    weighting factors are calculated according to the CFL paper
    where W per image (single channel) in minibatch = total number of pixels/ 
    number of positive or negative labels in that image 
    '''
    #print(torch.max(gt[0][0]),torch.max(gt[1][0]),torch.max(gt[2][0]),torch.max(gt[3][0]))
    vb = gt.le(0.0).float()
    vs = gt.gt(0.0).float()
    nb = torch.sum(vb,dim=(2,3))+1
    ns = torch.sum(vs,dim=(2,3))+1
    total_pix=nb+ns+1
    pb = nb/total_pix
    ps = ns/total_pix
    
    LogitsLoss= nn.BCEWithLogitsLoss()
    ponderedSCELoss=LogitsLoss(pred,gt)
    pond = torch.mul(vs.permute(2,3,0,1),1/ps) + torch.mul(vb.permute(2,3,0,1),1/pb)
    pond = pond.permute(2,3,0,1)
    ponderedSCELoss = ponderedSCELoss * pond
    loss = torch.mean(ponderedSCELoss)

    """
    pos_inds = gt.ge(0.1).float()
    neg_inds = gt.lt(0.1).float()
    N = (torch.numel(gt[0][0]))
    N_1 = (torch.sum((pos_inds==1.).float(),dim=(1,2,3)))
    N_0 = (torch.sum((neg_inds==1.).float(),dim=(1,2,3)))
    
    W_1 = N/N_1
    W_0 = N/N_0
    
    loss = 0

    pos_loss = W_1.view(-1,1,1,1) * (gt * -torch.log(pred))
    neg_loss = W_0.view(-1,1,1,1) * ((1 - gt)*(-torch.log(1-pred)))

    pos_loss = pos_loss.sum()
    neg_loss = neg_loss.sum()
    loss = pos_loss + neg_loss
    """
    
    return loss

class CELoss(nn.Module):
  '''nn.Module warpper for custom CE loss'''
  def __init__(self):
    super(CELoss, self).__init__()
    self.ce_loss = ce_loss

  def forward(self, out, target):
    return self.ce_loss(out, target)

class SUN360Dataset(Dataset):
    

    def __init__(self, file, transform=None, target_transform=None, joint_transform=None):
        """
        Args:
            json_file (string): Path to the json file with annotations.
            transform (callable, optional): Optional transform to be applied
                on an image.
            target_file (callable, optional): Optional transform to be applied
                on a map (edge and corner).    
        """
    
        self.images_data = pd.read_json(file)    
        self.transform = transform
        self.target_transform = target_transform
        self.joint_transform = joint_transform

    def __len__(self):
        return len(self.images_data)

    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()

        img_name = self.images_data.iloc[idx, 0]                        
        EM_name = self.images_data.iloc[idx, 1]
        CM_name = self.images_data.iloc[idx, 2]
        CL_name = self.images_data.iloc[idx, 3]
        image = Image.open(img_name)
        EM = Image.open(EM_name)
        CM = Image.open(CM_name)
        with open(CL_name, mode='r') as f:
            cor = np.array([line.strip().split() for line in f], np.int32)
        if(len(cor)%2 != 0) :
            print (CL_name.split('/')[-1])    
        
        """
        EM = np.asarray(EM)
        EM = np.expand_dims(EM, axis=2)
        CM = np.asarray(CM) 
        CM = np.expand_dims(CM, axis=2) 
        gt = np.concatenate((EM,CM),axis = 2)
        maps = Image.fromarray(gt)
        """
        
        if self.transform is not None:
            image = self.transform(image)
        
        if self.target_transform is not None:
            CM = self.target_transform(CM)
            EM = self.target_transform(EM)
        if self.joint_transform is not None:   
            image, EM, CM, cor = self.joint_transform([image, EM, CM, cor])      
        
        return image, EM, CM
"""
The SplitDataset class is used to split training or test set further to make
train/test/dev or train/val/test split. For SUN360 because of the small size
only train/test split will be used.
"""
class SplitDataset(Dataset):
    

    def __init__(self, dataset, transform=None, target_transform=None, joint_transform=None):
        """
        Args:
            json_file (string): Path to the json file with annotations.
            transform (callable, optional): Optional transform to be applied
                on an image.
            target_file (callable, optional): Optional transform to be applied
                on a map (edge and corner).    
        """
    
        self.images_data = dataset 
        self.transform = transform
        self.target_transform = target_transform
        self.joint_transform = joint_transform

    def __len__(self):
        return len(self.images_data)

    def __getitem__(self, idx):
        
        image, EM, CM = self.images_data[idx]
        #EM = self.images_data[idx,1]
        #CM = self.images_data[idx,2]

        """
        EM = np.asarray(EM)
        EM = np.expand_dims(EM, axis=2)
        CM = np.asarray(CM) 
        CM = np.expand_dims(CM, axis=2) 
        gt = np.concatenate((EM,CM),axis = 2)
        maps = Image.fromarray(gt)
        """
        
        if self.transform is not None:
            image = self.transform(image)
        
        if self.target_transform is not None:
            CM = self.target_transform(CM)
            EM = self.target_transform(EM)    
        
        if self.joint_transform is not None:
            image, EM, CM = self.joint_transform([image, EM, CM])

        return image, EM, CM

def map_loss(inputs, EM_gt,CM_gt,criterion):
    '''
    function to calculate total loss according to CFL paper
    '''
    EMLoss=0.
    CMLoss=0.
    for key in inputs:
        output = inputs[key] + eps
        EM=F.interpolate(EM_gt,size=(output.shape[-2],output.shape[-1]),mode='bilinear',align_corners=True)
        CM=F.interpolate(CM_gt,size=(output.shape[-2],output.shape[-1]),mode='bilinear',align_corners=True)
        edges,corners =torch.chunk(output,2,dim=1)
        EMLoss += criterion(edges,EM)
        CMLoss += criterion(corners,CM)        
    return EMLoss, CMLoss

def convert_to_images(inputs,epoch,phase):
    if not os.path.isdir("CM_pred"):
        os.mkdir("CM_pred")
    if not os.path.isdir("EM_pred"):
        os.mkdir("EM_pred")   

    tojpg = transforms.ToPILImage()    
    output = inputs['output'] + eps
    output = torch.sigmoid(output)
    edges,corners = torch.chunk(output,2,dim=1)
    image = corners[0].detach().cpu()
    image = torch.squeeze(image)
    image = tojpg(image)

    image1 = edges[0].detach().cpu()
    image1 = torch.squeeze(image1)
    image1 = tojpg(image1)

    if len(str(epoch)) == 1:
        epochstr = "000" + str(epoch)
    elif len(str(epoch)) == 2:
        epochstr = "00" + str(epoch)
    elif len(str(epoch)) == 3:
        epochstr = "0" + str(epoch)
    else :
        epochstr = str(epoch)            
    image.save("CM_pred/epoch_{}_{}_CM.jpg".format(epochstr,phase))
    image1.save("EM_pred/epoch_{}_{}_EM.jpg".format(epochstr,phase))

def map_predict(outputs, EM_gt,CM_gt):
    '''
    function to calculate total loss according to CFL paper
    '''
    output= outputs['output'] + eps
    output = torch.sigmoid(output)
    EM=F.interpolate(EM_gt,size=(output.shape[-2],output.shape[-1]),mode='bilinear',align_corners=True)
    CM=F.interpolate(CM_gt,size=(output.shape[-2],output.shape[-1]),mode='bilinear',align_corners=True)
    edges,corners =torch.chunk(output,2,dim=1)
    P_e, R_e, Acc_e, f1_e, IoU_e = evaluate(edges,EM)
    print('EDGES: IoU: ' + str('%.3f' % IoU_e) + '; Accuracy: ' + str('%.3f' % Acc_e) + '; Precision: ' + str('%.3f' % P_e) + '; Recall: ' + str('%.3f' % R_e) + '; f1 score: ' + str('%.3f' % f1_e))
    P_c, R_c, Acc_c, f1_c, IoU_c = evaluate(corners, CM)
    print('CORNERS: IoU: ' + str('%.3f' % IoU_c) + '; Accuracy: ' + str('%.3f' % Acc_c) + '; Precision: ' + str('%.3f' % P_c) + '; Recall: ' + str('%.3f' % R_c) + '; f1 score: ' + str('%.3f' % f1_c))


def _train(args):
    """
    is_distributed = len(args.hosts) > 1 and args.dist_backend is not None
    logger.debug("Distributed training - {}".format(is_distributed))

    if is_distributed:
        # Initialize the distributed environment.
        world_size = len(args.hosts)
        os.environ['WORLD_SIZE'] = str(world_size)
        host_rank = args.hosts.index(args.current_host)
        os.environ['RANK'] = str(host_rank)
        dist.init_process_group(backend=args.dist_backend, rank=host_rank, world_size=world_size)
        logger.info(
            'Initialized the distributed environment: \'{}\' backend on {} nodes. '.format(
                args.dist_backend,
                dist.get_world_size()) + 'Current host rank is {}. Using cuda: {}. Number of gpus: {}'.format(
                dist.get_rank(), torch.cuda.is_available(), args.num_gpus))
    """            
   
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    #device = 'cpu'
    logger.info("Device Type: {}".format(device))
    img_size = [128,256]
    logger.info("Loading SUN360 dataset")
    
    train_transform = transforms.Compose(
        [transforms.Resize((img_size[0],img_size[1])),
         transforms.ToTensor(),
         transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        ])
    train_target_transform = transforms.Compose([transforms.Resize((img_size[0],img_size[1])),
                                           transforms.ToTensor()])
    
    roll_gen = mytransforms.RandomHorizontalRollGenerator()
    flip_gen = mytransforms.RandomHorizontalFlipGenerator()
    #panostretch_gen = mytransforms.RandomPanoStretchGenerator(max_stretch = 2.0)
    #panostretch_gen,
    #[mytransforms.RandomPanoStretch(panostretch_gen), mytransforms.RandomPanoStretch(panostretch_gen), mytransforms.RandomPanoStretch(panostretch_gen), None]
    train_joint_transform = mytransforms.Compose([
                                       [transforms.Resize((img_size[0],img_size[1])),transforms.Resize((img_size[0],img_size[1])),transforms.Resize((img_size[0],img_size[1])),None],
                                       flip_gen,
                                       [mytransforms.RandomHorizontalFlip(flip_gen,p=0.5),mytransforms.RandomHorizontalFlip(flip_gen,p=0.5),mytransforms.RandomHorizontalFlip(flip_gen,p=0.5), None],
                                       [transforms.ToTensor(),transforms.ToTensor(),transforms.ToTensor(), None],
                                       [transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]), None, None, None],
                                       roll_gen,
                                       [mytransforms.RandomHorizontalRoll(roll_gen,p=0.5),mytransforms.RandomHorizontalRoll(roll_gen,p=0.5),mytransforms.RandomHorizontalRoll(roll_gen,p=0.5),None],
                                       [transforms.RandomErasing(p=0.5,scale=(0.01,0.02),ratio=(0.3,3.3),value=0), None, None, None],
                                       ])                                        

    valid_transform = transforms.Compose(
        [transforms.Resize((img_size[0],img_size[1])),
         transforms.ToTensor(),
         transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])
    valid_target_transform = transforms.Compose([transforms.Resize((img_size[0],img_size[1])),
                                           transforms.ToTensor()])     

    """
    #uncomment this block if train/val split is needed
    indices = list(range(len(trainvalidset)))
    split = int(np.floor(len(trainvalidset)*0.8))
    train_idx = indices[:10]
    valid_idx = indices[10:]
    train = Subset(trainvalidset, train_idx)
    valid = Subset(trainvalidset, valid_idx)
    trainset = SplitDataset(train, transform = None, target_transform = None, joint_transform=train_joint_transform)
    """
    trainset = SUN360Dataset(file="traindata.json",transform = None, target_transform = None, joint_transform=train_joint_transform)
    train_loader = DataLoader(trainset, batch_size=args.batch_size,
                                               shuffle=True, num_workers=args.workers)
    
    #supplement= SUN360Dataset('morethan4corners.json',transform=None,target_transform=None,joint_transform=train_joint_transform)
    #suppl_loader = DataLoader(supplement, batch_size=1,
    #                                           shuffle=True, num_workers=2)

    validset = SUN360Dataset(file="testdata.json",transform = valid_transform, target_transform = valid_target_transform, joint_transform=None)
    valid_loader = DataLoader(validset, batch_size=args.batch_size,
                                              shuffle=False, num_workers=args.workers)
     
    logger.info("Model loaded")
    if args.conv_type == "Std":
        model = StdConvsCFL(args.model_name,conv_type=args.conv_type, layerdict=None, offsetdict=None)
    elif args.conv_type == "Equi":                                       
        layerdict, offsetdict = torch.load('layertrain.pt'), torch.load('offsettrain.pt')
        model = EquiConvsCFL(args.model_name,conv_type=args.conv_type, layerdict=layerdict, offsetdict=offsetdict)    

    if torch.cuda.device_count() > 1:
        logger.info("Gpu count: {}".format(torch.cuda.device_count()))
        model = nn.DataParallel(model)
    
    model = model.to(device)
    criterion = CELoss().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr,weight_decay=0.0005)
    LR_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer,0.995)
    writer= SummaryWriter(log_dir="{}".format(args.logdir),comment="visualising losses of training and validation")

    for epoch in progressbar(range(1, args.epochs+1),redirect_stdout=True):
        epochtime1=time.time()
        # training phase
        phase = 'train'
        running_loss = 0.0
        for i, data in enumerate(train_loader):
            # get the inputs
            inputs, EM , CM = data
            """
            '''this code block is to add one example of a room with 
            more than 4 floor-ceiling corner pairs to each batch '''
            RGBsup,EMsup,CMsup = next(itertools.cycle(suppl_loader))
            inputs = torch.cat([inputs,RGBsup],dim=0)
            EM = torch.cat([EM,EMsup],dim=0)
            CM = torch.cat([CM,CMsup],dim=0)
            """
            inputs, EM, CM = inputs.to(device), EM.to(device), CM.to(device)

            # zero the parameter gradients
            optimizer.zero_grad()

            # forward + backward + optimize
            model.train()
            outputs = model(inputs)
            if(epoch%10 == 0 and i == 0):
                convert_to_images(outputs,epoch,phase)
            EMLoss, CMLoss = map_loss(outputs,EM,CM,criterion)
            loss = EMLoss + CMLoss
            loss.backward()
            optimizer.step()

            # print statistics
            running_loss += loss.item()
            """
            if i % 1 == 0:  # print every 1 mini-batches
                print('[%d, %5d] loss: %.3f' %
                      (epoch, i + 1, running_loss / args.batch_size))
                running_loss = 0.0
            """
        epoch_loss = running_loss / len(trainset)   
        print("epoch: {}".format(epoch),", train_loss: %.3f" %(epoch_loss))
        writer.add_scalar("training_loss", epoch_loss,epoch)
    
        # validation phase
        if(epoch%1==0):
            phase = 'val'
            with torch.no_grad():
                running_loss = 0.0
                for i, data in enumerate(valid_loader):
                    # get the inputs
                    inputs, EM , CM = data
                    inputs, EM, CM = inputs.to(device), EM.to(device), CM.to(device)
                    model.eval()
                    outputs = model(inputs)
                    if(epoch%10 == 0 and i == 0):
                        convert_to_images(outputs,epoch,phase)
                    EMLoss, CMLoss = map_loss(outputs,EM,CM,criterion)
                    loss = EMLoss + CMLoss
                    # print statistics
                    running_loss += loss.item()
                    #map_predict(outputs,EM,CM)
                      
                epoch_loss = running_loss / len(validset)    
                print("epoch: {}".format(epoch),", valid_loss: %.3f" %(epoch_loss))
                writer.add_scalar("validation loss",epoch_loss,epoch)
        if (epoch%100==0):
            _save_model(model, args.model_dir, epoch)        
        LR_scheduler.step()  
        epochtime2 = time.time()
    epochdiff = epochtime2 - epochtime1          
    writer.close()   
    print ("time for 1 complete epoch: ", epochdiff)      
    print('Finished Training')
    


def _save_model(model, model_dir, epoch):
    logger.info("Saving the model.")
    modelfile = "model_epoch"+str(epoch)+".pth"
    path = os.path.join(model_dir, modelfile)
    # recommended way from http://pytorch.org/docs/master/notes/serialization.html
    torch.save(model.cpu().state_dict(), path)
    model.cuda()


def model_fn(model_dir,model_name):
    logger.info('model_fn')
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = EquiConvsCFL(model_name,conv_type='Equi')
    if torch.cuda.device_count() > 1:
        logger.info("Gpu count: {}".format(torch.cuda.device_count()))
        model = nn.DataParallel(model)

    with open(os.path.join(model_dir, 'model_epoch300.pth'), 'rb') as f:
        model.load_state_dict(torch.load(f))
    return model.to(device)


if __name__ == '__main__':
    
    
    parser = argparse.ArgumentParser()

    parser.add_argument('--workers', type=int, default=2, metavar='W',
                        help='number of data loading workers (default: 2)')
    parser.add_argument('--epochs', type=int, default=1, metavar='E',
                        help='number of total epochs to run (default: 1)')
    parser.add_argument('--batch_size', type=int, default=4, metavar='BS',
                        help='batch size (default: 4)')
    parser.add_argument('--lr', type=float, default=2.5e-4, metavar='LR',
                        help='initial learning rate (default: 2.5e-4)')
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M', help='momentum (default: 0.9)')
    parser.add_argument('--model-dir', type=str, default="")
    parser.add_argument('--model-name', type=str,default="efficientnet-b0")
    parser.add_argument('--conv_type', type=str,default="Std", help='select convolution type between Std and Equi. Also determines the network type')
    parser.add_argument('--logdir', type=str,default="exp/StdConvsCFL_100epochs", help='save directory for tensorboard event files')
    #parser.add_argument('--dist_backend', type=str, default='gloo', help='distributed backend (default: gloo)')

    #env = sagemaker_containers.training_env()
    #parser.add_argument('--hosts', type=list, default=env.hosts)
    #parser.add_argument('--current-host', type=str, default=env.current_host)
    #parser.add_argument('--model-dir', type=str, default=env.model_dir)
    #parser.add_argument('--data-dir', type=str, default=env.channel_input_dirs.get('training'))
    #parser.add_argument('--num-gpus', type=int, default=env.num_gpus)
    time1= time.time()
    _train(parser.parse_args())
    time2=time.time()
    diff = time2 - time1
    print("total execution time: ",diff," seconds")
    print("total execution time: ",diff/60," minutes")