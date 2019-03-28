import argparse
import cmath
import logging
import math
import pathlib
import random
import shutil

import h5py
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.cuda as cuda
import torch.nn as nn
from skimage.measure import compare_ssim as ssim
from tensorboardX import SummaryWriter
from torch.autograd import Variable
from torch.nn import functional as F
from torch.utils.data import Dataset

import utils
from data import transforms
from anet_model import AnetModel

parser = argparse.ArgumentParser()

parser.add_argument("--data-path", help="The path of the directory to the dataset", required=True)
parser.add_argument("--mode", help="Choice of space to find the nearest neighbour", choices=['image', 'kspace'], default='image')
parser.add_argument("--center-fractions", nargs='+', default=[0.08, 0.04])
parser.add_argument("--accelerations", nargs='+', default=[4, 8])
parser.add_argument("--resolution", default=320, type=int)
parser.add_argument("--sample-rate", default=1.)
parser.add_argument("--challenge", default="singlecoil", choices=["singlecoil", "multicoil"])

parser.add_argument("--batch-size", default=1, type=int)
parser.add_argument("--learning-rate", default=0.0001, type=float)
parser.add_argument("--epoch", default=10, type=int)
parser.add_argument("--reluslope", default=0.2, type=float)

parser.add_argument("--checkpoint", default='DAE/best_model.pt')
parser.add_argument("--exp-dir", default='DAE')
parser.add_argument("--resume", default=False, type=bool, choices=[True, False])
parser.add_argument("--device", default="cuda", choices=["cpu", "cuda"])
parser.add_argument('--num-chans', type=int, default=32, help='Number of U-Net channels')
parser.add_argument('--num-pools', type=int, default=4, help='Number of U-Net pooling layers')
parser.add_argument('--drop-prob', type=float, default=0.0, help='Dropout probability')
parser.add_argument('--weight-decay', type=float, default=0., help='Strength of weight decay regularization')

args = parser.parse_args()
train_loader, dev_loader = utils.create_data_loaders(args)    

# ### Custom dataset class

def build_model(args):
    model = AnetModel(
        in_chans=2,
        out_chans=2,
        chans=args.num_chans,
        num_pool_layers=args.num_pools,
        drop_prob=args.drop_prob
    ).to(args.device)
    return model

def build_optim(args, params):
    optimizer = torch.optim.RMSprop(params, args.learning_rate, weight_decay=args.weight_decay)
    return optimizer

def load_model(checkpoint_file):
    checkpoint = torch.load(checkpoint_file)
    args = checkpoint['args']
    model = build_model(args)
    model.load_state_dict(checkpoint['model'])

    optimizer = build_optim(args, model.parameters())
    optimizer.load_state_dict(checkpoint['optimizer'])
    return checkpoint, model, optimizer

# ### Image normalized

loss_func = nn.MSELoss()
best_val_loss = 1e9
writer = SummaryWriter(log_dir=args.exp_dir+'/summary')
valid_loss=[]
train_loss=[]
print('Total number of epochs:', args.epoch)
print('Total number of training iterations: ',len(train_loader))
print('Total number of validation iterations: ',len(dev_loader))

if args.resume:
    checkpoint, model, optimizer = load_model(args.checkpoint)
    best_dev_loss = checkpoint['best_dev_loss']
    start_epoch = checkpoint['epoch']
    if checkpoint['state']=='train':
        train = False
    del checkpoint
else:
    model = build_model(args)
    # if args.data_parallel:
        # model = torch.nn.DataParallel(model)
    optimizer = build_optim(args, model.parameters())
    best_dev_loss = 1e9
    start_epoch = 0
    train = True

# if args.resume:
#     checkpoint, model optimizer = load_model(args.checkpoint)
#     #best_val_loss = checkpoint['best_val_loss']
#     start_epoch = checkpoint['epoch']
#     if checkpoint['state']=='train':
#         train = False
#     del checkpoint
# else:
#     encoder = Encoder().cuda()
#     decoder = Decoder().cuda()
#     parameters = list(encoder.parameters())+ list(decoder.parameters())
#     optimizer = torch.optim.Adam(parameters, lr=args.learning_rate)
#     start_epoch = 0
#     train = True

for i in range(start_epoch, args.epoch):
    print("Epoch: ",i)
    global_step = i * len(train_loader) 
    ##########################################TRAINING PHASE######################################################
    if train:
        print("Training Phase")
        total_loss = 0.0
        model.train()
        for j,data in enumerate(train_loader):
            original_kspace,masked_kspace, mask = data
            #normalizing the kspace
            nmasked_kspace,mdivisor = utils.imagenormalize(masked_kspace)
            noriginal_kspace,odivisor = utils.imagenormalize(original_kspace,mdivisor)

            #transforming the input according to dimension and type 
            noriginal_kspace,nmasked_kspace = utils.transformshape(noriginal_kspace), utils.transformshape(nmasked_kspace)
            nmasked_kspace = Variable(nmasked_kspace).cuda()
            noriginal_kspace = Variable(noriginal_kspace).cuda()

            #setting up all the gradients to zero
            optimizer.zero_grad()
            
            #forward pass
            outputkspace = model(nmasked_kspace)
            
            #finding the kspace loss
            loss1 = loss_func(outputkspace, noriginal_kspace)
            
            # finding the corresponding images
            # print(noriginal_kspace.shape)
            original_3d_image = torch.tensor(transforms.ifft2(utils.transformback(noriginal_kspace)), requires_grad=True)
            output_3d_image = torch.tensor(transforms.ifft2(utils.transformback(outputkspace)), requires_grad=True)
            
            #finding the image loss
            loss2 = loss_func(original_3d_image, output_3d_image)
            
            #backward pass
            (loss1 + loss2).backward()
            
            optimizer.step()
            total_loss += loss1.data.item() + loss2.data.item()
            if j % 100 == 0:
                avg_loss = total_loss/(j+1)
                print('Avg training loss: ',avg_loss,' Training loss: ',loss1.data.item() + loss2.data.item(), ' iteration :', j+1)
                if j % 500 == 0:
                    utils.compareimageoutput(original_kspace,masked_kspace,outputkspace,mask,writer,global_step + j+1, 0)

            writer.add_scalar('TrainLoss', loss1.data.item() + loss2.data.item(), global_step + j+1)
        utils.save_model(args, args.exp_dir, i+1 , encoder,decoder, optimizer, best_val_loss, False, 'train')    
        train_loss.append(total_loss/len(train_loader))
    train = True
    
    ################################VALIDATION#######################################################
    print("Validation Phase")
    # validation loss
    total_val_loss = 0.0
    encoder.eval()
    decoder.eval()
    for j,data in enumerate(dev_loader):
        original_kspace,masked_kspace, mask = data
        #normalizing the kspace
        nmasked_kspace,mdivisor = imagenormalize(masked_kspace)
        noriginal_kspace,odivisor = imagenormalize(original_kspace)
        
        #transforming the input according dimention and type 
        noriginal_kspace,nmasked_kspace = transformshape(noriginal_kspace), transformshape(nmasked_kspace)
        nmasked_kspace = Variable(nmasked_kspace).cuda()
        noriginal_kspace = Variable(noriginal_kspace).cuda()
        
        #forward pass
        latent = encoder(nmasked_kspace)
        outputkspace = decoder(latent)
        
        #finding the loss
        loss = loss_func(outputkspace, noriginal_kspace)
        
        total_val_loss += loss.data.item()
        
        if j % 100 == 0:
            avg_loss = total_val_loss/(j+1)
            print('Avg Validation loss: ',avg_loss,' Validation loss: ',loss.data.item(), ' iteration :', j+1, 0)
            if j % 200 == 0:
                utils.compareimageoutput(original_kspace,masked_kspace,outputkspace,mask,writer,global_step + j+1, 0)
        
        writer.add_scalar('ValidationLoss', loss.data.item(), global_step + j+1)
        
    valid_loss.append(total_val_loss / len(dev_loader))
    
    print('saving')
    is_new_best = valid_loss[-1] < best_val_loss
    best_val_loss = min(best_val_loss, valid_loss[-1])
    print("best val loss :",best_val_loss)
    utils.save_model(args, args.exp_dir, i+1 , encoder,decoder, optimizer, best_val_loss, is_new_best, 'valid')    
writer.close()