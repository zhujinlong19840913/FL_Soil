import time
import numpy as np
import torch
import torch_npu
import torch.nn
from tqdm import trange
from data_gen import load_test_data_for_rnn,load_train_data_for_rnn,load_test_data_for_cnn, load_train_data_for_cnn,erath_data_transform,sea_mask_rnn,sea_mask_cnn
from loss import  NaNMSELoss, MeanBiasError,RelativeAbsoluteError,RelativeSquaredError,NormalizedRootMeanSquaredError,Huber1,RootMeanSquaredLogarithmicError,MeanAbsolutePercentError,RootMeanSquaredError,SquarMeanAbsolutePercentError,LogCosh,RelativeRootMeanSquaredError
from model import LSTMModel,CNN,ConvLSTMModel,TimeNetModel
from torch_npu.npu import amp


def train(x,
          y,
          static,
          mask, 
          scaler_x,
          scaler_y,
          cfg,
          num_repeat,
          PATH,
          out_path,
          device, 
          device_ids,
          num_task=None,
          valid_split=True):
   
    patience = cfg['patience']
    wait = 0
    best = 9999
    print('```````````````````````````````````````````````')
    x = x.astype(np.float32)
    y = y.astype(np.float32)
    static = static.astype(np.float32)
    # print(x.dtype)
    # print(y.dtype)
    # print(static.dtype)
    print('the device is {d}'.format(d=device))
    
    if cfg['modelname'] in ['CNN', 'ConvLSTM']:
#	Splice x according to the sphere shape
        lat_index,lon_index = erath_data_transform(cfg, x)
        print('\033[1;31m%s\033[0m' % "Applied Model is {m_n}, we need to transform the data according to the sphere shape".format(m_n=cfg['modelname']))
    if valid_split:
        nt,nf,nlat,nlon = x.shape  #x shape :nt,nf,nlat,nlon
	#Partition validation set and training set
        N = int(nt*cfg['split_ratio'])
        x_valid, y_valid, static_valid = x[N:], y[N:], static
        x, y = x[:N], y[:N]       

    lossmse = torch.nn.MSELoss()

#	filter Antatctica
    # print('x_train shape is', x.shape)
    # print('y_train shape is', y.shape)
    # print('static_train shape is', static.shape)
    # print('mask shape is', mask.shape)

    # mask see regions
    #Determine the land boundary
    if cfg['modelname'] in ['LSTM','TimeNetModel']:
        x, y, static = sea_mask_rnn(cfg, x, y, static, mask)
        x_valid, y_valid, static_valid = sea_mask_rnn(cfg, x_valid, y_valid, static_valid, mask)
    elif cfg['modelname'] in ['CNN','ConvLSTM']:
        x, y, static, mask_index = sea_mask_cnn(cfg, x, y, static, mask)

    # train and validate
    # NOTE: We preprare two callbacks for training:
    #       early stopping and save best model.
    for num_ in range(cfg['num_repeat']):
        # prepare models
	#Selection model
        if cfg['modelname'] in ['LSTM']:
            lstmmodel_cfg = {}
            lstmmodel_cfg['input_size'] = cfg["input_size"]
            lstmmodel_cfg['hidden_size'] = cfg["hidden_size"]*1
            lstmmodel_cfg['out_size'] = 1
            model = LSTMModel(cfg,lstmmodel_cfg).to(device)
            # model = torch.nn.DataParallel(model, device_ids=device_ids)
        elif cfg['modelname'] in ['CNN']:
            model = CNN(cfg).to(device)
        elif cfg['modelname'] in ['ConvLSTM']:
            model = ConvLSTMModel(cfg).to(device)
        elif cfg['modelname'] in ['TimeNetModel']:
            lstmmodel_cfg = {}
            lstmmodel_cfg['input_size'] = cfg["input_size"]
            lstmmodel_cfg['hidden_size'] = cfg["hidden_size"]*1
            lstmmodel_cfg['out_size'] = 1
            model = TimeNetModel(cfg,lstmmodel_cfg).to(device)

      #  model.train()
	 # Prepare for training
    # NOTE: Only use `Adam`, we didn't apply adaptively
    #       learing rate schedule. We found `Adam` perform
    #       much better than `Adagrad`, `Adadelta`.
        optim = torch.optim.Adam(model.parameters(),lr=cfg['learning_rate'])
        scaler = amp.GradScaler()
        # print(cfg["modelname"])

        with trange(1, cfg['epochs']+1) as pbar:
            for epoch in pbar:
                pbar.set_description(
                    cfg['modelname']+' '+str(num_repeat))
                t_begin = time.time()
                # train
                MSELoss = 0
                for iter in range(0, cfg["niter"]):
                    # print(iter)
                    # print(device)
 # ------------------------------------------------------------------------------------------------------------------------------
 #  train way for LSTM model
                    if cfg["modelname"] in ['LSTM']:
                        # generate batch data for Recurrent Neural Network
                        x_batch, y_batch, aux_batch, _, _ = \
                            load_train_data_for_rnn(cfg, x, y, static, scaler_y)
                        x_batch = torch.from_numpy(x_batch).to(device)                        
                        aux_batch = torch.from_numpy(aux_batch).to(device)
                        # print(x_batch.size(),aux_batch.size(),'---')
                        y_batch = torch.from_numpy(y_batch).to(device)
                        aux_batch = aux_batch.unsqueeze(1)
                        aux_batch = aux_batch.repeat(1,x_batch.shape[1],1)
                        
                        x_batch = torch.cat([x_batch, aux_batch], 2)
                        # print(x_batch.size())
                        pred = model(x_batch, aux_batch)
                        pred = torch.squeeze(pred,1)
 #  train way for CNN model
                    elif cfg['modelname'] in ['CNN']:
                        # generate batch data for Convolutional Neural Network
                        x_batch, y_batch, aux_batch, _, _ = \
                            load_train_data_for_cnn(cfg, x, y, static, scaler_y,lat_index,lon_index,mask_index)
                        x_batch[np.isnan(x_batch)] = 0  # filter nan values to train cnn model
                        x_batch = torch.from_numpy(x_batch).to(device)
                        aux_batch = torch.from_numpy(aux_batch).to(device)
                        y_batch = torch.from_numpy(y_batch).to(device)
                        x_batch = x_batch.squeeze(dim=1)
                        x_batch = x_batch.reshape(x_batch.shape[0],x_batch.shape[1]*x_batch.shape[2],x_batch.shape[3],x_batch.shape[4])
                        x_batch = torch.cat([x_batch, aux_batch], 1)
                        pred = model(x_batch, aux_batch)
                    elif cfg["modelname"] in ['TimeNetModel']:
                        # generate batch data for Recurrent Neural Network
                        x_batch, y_batch, aux_batch, _, _ = \
                            load_train_data_for_rnn(cfg, x, y, static, scaler_y)
                        x_batch = torch.from_numpy(x_batch).to(device)                        
                        aux_batch = torch.from_numpy(aux_batch).to(device)
                        # print(x_batch.size(),aux_batch.size(),'---')
                        y_batch = torch.from_numpy(y_batch).to(device)
                        aux_batch = aux_batch.unsqueeze(1)
                        aux_batch = aux_batch.repeat(1,x_batch.shape[1],1)
                        
                        x_batch = torch.cat([x_batch, aux_batch], 2)
                        # print(x_batch.size())
                        pred = model(x_batch, aux_batch)
                        pred = torch.squeeze(pred,1)
                        
                    elif cfg['modelname'] in ['ConvLSTM']:
                        # generate batch data for Convolutional LSTM
                        x_batch, y_batch, aux_batch, _, _ = \
                            load_train_data_for_cnn(cfg, x, y, static, scaler_y,lat_index,lon_index,mask_index) # same as Convolutional Neural Network
                        # print(x_batch.shape,'----------0---------')
                        x_batch[np.isnan(x_batch)] = 0  # filter nan values to train cnn model
                        # print(x_batch,y_batch)
                        # print(x_batch.shape,'----------1---------')
                        
                        # x_batch = torch.from_numpy(x_batch).to(device)
                        # aux_batch = torch.from_numpy(aux_batch).to(device)
                        # y_batch = torch.from_numpy(y_batch).to(device)
                        # aux_batch = aux_batch.unsqueeze(1)
                        # aux_batch = aux_batch.repeat(1,x_batch.shape[1],1,1,1)
                        # x_batch = x_batch.squeeze(dim=1)
                        # print(x_batch.shape,aux_batch.shape,'------------')
                        
                        
                        
                        # print(aux_batch.shape,'------a-----')
                        aux_batch = np.expand_dims(aux_batch,1)
                        # print(aux_batch.shape,'------b-----')                        
                        aux_batch = np.repeat(aux_batch,x_batch.shape[1],axis=1)
                        # print(aux_batch.shape,'------c-----')                        
                        x_batch = np.concatenate((x_batch, aux_batch), axis=2)
                        # print(x_batch.shape,'-------4----------')                       
                        
                        x_batch = torch.from_numpy(x_batch).to(device)
                        y_batch = torch.from_numpy(y_batch).to(device)
                        x_batch = x_batch.squeeze(dim=1)
                        # print(x_batch.shape,'----------2---------')
                        with amp.autocast():
                            pred = model(x_batch.float(), None,cfg)
                            # print('-------1-------',pred.shape)
                            # print('------------2-----------',torch.isnan(pred).any())
                            loss = Huber1.fit(cfg, pred.float(), y_batch.float())
                        optim.zero_grad()
                        scaler.scale(loss.float()).backward()
                        scaler.step(optim)
                        scaler.update()
                        MSELoss += loss.item()

                    # loss.backward()
                    
                        # pred = model(x_batch, aux_batch,cfg)
 # ------------------------------------------------------------------------------------------------------------------------------
                    # loss = NaNMSELoss.fit(cfg, pred.float(), y_batch.float(),lossmse)
                    # loss = MeanBiasError.fit(cfg, pred.float(), y_batch.float())
                    # loss =RelativeAbsoluteError.fit(cfg, pred.float(), y_batch.float())
                    #loss = RelativeSquaredError.fit(cfg, pred.float(), y_batch.float())
                    #loss = NormalizedRootMeanSquaredError.fit(cfg, pred.float(), y_batch.float())
                    # loss = LogCosh.fit(cfg, pred.float(), y_batch.float())
                    loss = RelativeRootMeanSquaredError.fit(cfg, pred.float(), y_batch.float())
                    
                    
                    
#                     with amp.autocast():
                    # loss = Huber1.fit(cfg, pred.float(), y_batch.float())
                    # loss = RootMeanSquaredLogarithmicError.fit(cfg, pred.float(), y_batch.float())
#                     #loss = MeanBiasError.fit(cfg, pred.float(), y_batch.float())
#                     # loss = MeanAbsolutePercentError.fit(cfg, pred.float(), y_batch.float())
                    # loss = RootMeanSquaredError.fit(cfg, pred.float(), y_batch.float())
                    # loss = SquarMeanAbsolutePercentError.fit(cfg, pred.float(), y_batch.float())
                    optim.zero_grad()
#                     scaler.scale(loss).backward()

                    loss.backward()
#                     scaler.step(optim)
#                     scaler.update()
                    optim.step()                    
                    MSELoss += loss.item()
# ------------------------------------------------------------------------------------------------------------------------------
                t_end = time.time()
                # get loss log
                loss_str = "Epoch {} Train MSE Loss {:.3f} time {:.2f}".format(epoch, MSELoss / cfg["niter"], t_end - t_begin)
                print(loss_str)
                # validate
		#Use validation sets to test trained models
		#If the error is smaller than the minimum error, then save the model.
                if valid_split:
                    del x_batch, y_batch, aux_batch
                    MSE_valid_loss = 0
                    if epoch % 20 == 0:
                        wait += 1
                        # NOTE: We used grids-mean NSE as valid metrics.
                        t_begin = time.time()
# ------------------------------------------------------------------------------------------------------------------------------
 #  validate way for LSTM model
                        if cfg["modelname"] in ['LSTM','TimeNetModel']:
                            gt_list = [i for i in range(0,x_valid.shape[0]-cfg['seq_len'],cfg["stride"])]
                            n = (x_valid.shape[0]-cfg["seq_len"])//cfg["stride"]
                            for i in range(0, n):
                                #mask
                                x_valid_batch, y_valid_batch, aux_valid_batch, _, _ = \
                         load_test_data_for_rnn(cfg, x_valid, y_valid, static_valid, scaler_y,cfg["stride"], i, n)                              
                                x_valid_batch = torch.Tensor(x_valid_batch).to(device)
                                y_valid_batch = torch.Tensor(y_valid_batch).to(device)
                                aux_valid_batch = torch.Tensor(aux_valid_batch).to(device)
                                aux_valid_batch = aux_valid_batch.unsqueeze(1)
                                aux_valid_batch = aux_valid_batch.repeat(1,x_valid_batch.shape[1],1)
                                x_valid_batch = torch.cat([x_valid_batch, aux_valid_batch], 2)
                                with torch.no_grad():
                                    pred_valid = model(x_valid_batch, aux_valid_batch)
                                # mse_valid_loss = NaNMSELoss.fit(cfg, pred_valid.squeeze(1), y_valid_batch,lossmse)
                                # mse_valid_loss = MeanBiasError.fit(cfg, pred_valid.squeeze(1), y_valid_batch)
                                # mse_valid_loss =RelativeAbsoluteError.fit(cfg, pred_valid.squeeze(1), y_valid_batch)
                                #mse_valid_loss = RelativeSquaredError.fit(cfg, pred_valid.squeeze(1), y_valid_batch)
                                #mse_valid_loss = NormalizedRootMeanSquaredError.fit(cfg,  pred_valid.squeeze(1), y_valid_batch)
                                # mse_valid_loss = Huber1.fit(cfg, pred_valid.squeeze(1), y_valid_batch)
                                #mse_valid_loss = RootMeanSquaredLogarithmicError.fit(cfg, pred_valid.squeeze(1), y_valid_batch)
                                #mse_valid_loss = MeanBiasError.fit(cfg, pred_valid.squeeze(1), y_valid_batch)
                                # mse_valid_loss = MeanAbsolutePercentError.fit(cfg, pred_valid.squeeze(1), y_valid_batch)
                                # mse_valid_loss = RootMeanSquaredError.fit(cfg, pred_valid.float(), y_valid_batch.float())
                                # mse_valid_loss = LogCosh.fit(cfg, pred_valid.squeeze(1), y_valid_batch)
                                mse_valid_loss = RelativeRootMeanSquaredError.fit(cfg, pred_valid.squeeze(1), y_valid_batch)
                                # mse_valid_loss = SquarMeanAbsolutePercentError.fit(cfg, pred_valid.float(), y_valid_batch.float())
                                MSE_valid_loss += mse_valid_loss.item()
#  validate way for CNN model
                        elif cfg['modelname'] in ['CNN']:
                            gt_list = [i for i in range(0,x_valid.shape[0]-cfg['seq_len']-cfg['forcast_time'],cfg["stride"])]
                            valid_batch_size = cfg["batch_size"]*10
                            for i in gt_list:
                                x_valid_batch, y_valid_batch, aux_valid_batch, _, _ = \
                         load_test_data_for_cnn(cfg, x_valid, y_valid, static_valid, scaler_y,gt_list,lat_index,lon_index, i ,cfg["stride"]) # same as Convolutional Neural Network

                                x_valid_batch[np.isnan(x_valid_batch)] = 0
                                x_valid_batch = torch.Tensor(x_valid_batch).to(device)
                                y_valid_batch = torch.Tensor(y_valid_batch).to(device)
                                aux_valid_batch = torch.Tensor(aux_valid_batch).to(device)
                                # x_valid_temp = torch.cat([x_valid_temp, static_valid_temp], 2)
                                x_valid_batch = x_valid_batch.squeeze(1)
                                x_valid_batch = x_valid_batch.reshape(x_valid_batch.shape[0],x_valid_batch.shape[1]*x_valid_batch.shape[2],x_valid_batch.shape[3],x_valid_batch.shape[4])
                                x_valid_batch = torch.cat([x_valid_batch, aux_valid_batch], axis=1)
                                with torch.no_grad():
                                    pred_valid = model(x_valid_batch, aux_valid_batch)
                                mse_valid_loss = NaNMSELoss.fit(cfg, pred_valid, y_valid_batch,lossmse)
                                #mse_valid_loss = MeanBiasError.fit(cfg, pred_valid, y_valid_batch)
                                #mse_valid_loss =RelativeAbsoluteError.fit(cfg, pred_valid, y_valid_batch)
                                #mse_valid_loss = RelativeSquaredError.fit(cfg, pred_valid, y_valid_batch)
                                #mse_valid_loss = NormalizedRootMeanSquaredError.fit(cfg, pred_valid, y_valid_batch)
                                # mse_valid_loss = Huber1.fit(cfg, pred_valid, y_valid_batch)
                                #mse_valid_loss = RootMeanSquaredLogarithmicError.fit(cfg, pred_valid, y_valid_batch)
                                #mse_valid_loss = MeanBiasError.fit(cfg, pred_valid, y_valid_batch)
                                # mse_valid_loss = MeanAbsolutePercentError.fit(cfg, pred_valid, y_valid_batch)
                                MSE_valid_loss += mse_valid_loss.item()
#  validate way for ConvLSTM model，same as CNN model
                        elif cfg['modelname'] in ['ConvLSTM']:
                            gt_list = [i for i in range(0,x_valid.shape[0]-cfg['seq_len']-cfg['forcast_time'],cfg["stride"])]
                            valid_batch_size = cfg["batch_size"]*10
                            for i in gt_list:
                                x_valid_batch, y_valid_batch, aux_valid_batch, _, _ = \
                         load_test_data_for_cnn(cfg, x_valid, y_valid, static_valid, scaler_y,gt_list,lat_index,lon_index, i ,cfg["stride"]) # same as Convolutional Neural Network

                                x_valid_batch[np.isnan(x_valid_batch)] = 0
                                x_valid_batch = torch.Tensor(x_valid_batch).to(device)
                                y_valid_batch = torch.Tensor(y_valid_batch).to(device)
                                aux_valid_batch = torch.Tensor(aux_valid_batch).to(device)
                                aux_valid_batch = aux_valid_batch.unsqueeze(1)
                                aux_valid_batch = aux_valid_batch.repeat(1,x_valid_batch.shape[1],1,1,1)
                                # x_valid_temp = torch.cat([x_valid_temp, static_valid_temp], 2)
                                x_valid_batch = x_valid_batch
                                with torch.no_grad():
                                    pred_valid = model(x_valid_batch, aux_valid_batch,cfg)
                                mse_valid_loss = NaNMSELoss.fit(cfg, pred_valid, y_valid_batch,lossmse)
                                #mse_valid_loss = MeanBiasError.fit(cfg, pred_valid, y_valid_batch)
                                #mse_valid_loss =RelativeAbsoluteError.fit(cfg, pred_valid, y_valid_batch)
                                #mse_valid_loss = RelativeSquaredError.fit(cfg, pred_valid, y_valid_batch)
                                #mse_valid_loss = NormalizedRootMeanSquaredError.fit(cfg, pred_valid, y_valid_batch)
                                # mse_valid_loss = Huber1.fit(cfg, pred_valid, y_valid_batch)
                                #mse_valid_loss = RootMeanSquaredLogarithmicError.fit(cfg, pred_valid, y_valid_batch)
                                #mse_valid_loss = MeanBiasError.fit(cfg, pred_valid, y_valid_batch)
                                # mse_valid_loss = MeanAbsolutePercentError.fit(cfg, pred_valid, y_valid_batch)
                                MSE_valid_loss += mse_valid_loss.item()
# ------------------------------------------------------------------------------------------------------------------------------
             

                        t_end = time.time()
                        mse_valid_loss = MSE_valid_loss/(len(gt_list))
                        # get loss log
                        loss_str = '\033[1;31m%s\033[0m' % \
                                "Epoch {} Val MSE Loss {:.3f}  time {:.2f}".format(epoch,mse_valid_loss, 
                                    t_end-t_begin)
                        print(loss_str)
                        val_save_acc = mse_valid_loss

                        # save best model by val loss
                        # NOTE: save best MSE results get `single_task` better than `multi_tasks`
                        #       save best NSE results get `multi_tasks` better than `single_task`
                        if val_save_acc < best:
                        #if MSE_valid_loss < best:
                            torch.save(model,out_path+cfg['modelname']+'_para.pkl')
                            wait = 0  # release wait
                            best = val_save_acc #MSE_valid_loss
                            print('\033[1;31m%s\033[0m' % f'Save Epoch {epoch} Model')
                else:
                    # save best model by train loss
                    if MSELoss < best:
                        best = MSELoss
                        wait = 0
                        torch.save(model,out_path+cfg['modelname']+'_para.pkl')
                        print('\033[1;31m%s\033[0m' % f'Save Epoch {epoch} Model')

                # early stopping
                if wait >= patience:
                    return
            return


