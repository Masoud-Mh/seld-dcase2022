#
# A wrapper script that trains the SELDnet. The training stops when the early stopping metric - SELD error stops improving.
#

import os
import sys
import numpy as np
import matplotlib.pyplot as plot
import cls_feature_class
import cls_data_generator
import seldnet_model
import parameters
import time
from time import gmtime, strftime
import torch
import torch.nn as nn
import torch.optim as optim
plot.switch_backend('agg')
from IPython import embed
from cls_compute_seld_results import ComputeSELDResults, reshape_3Dto2D

def get_accdoa_labels(accdoa_in, nb_classes):
    x, y, z = accdoa_in[:, :, :nb_classes], accdoa_in[:, :, nb_classes:2*nb_classes], accdoa_in[:, :, 2*nb_classes:]
    sed = np.sqrt(x**2 + y**2 + z**2) > 0.5
      
    return sed, accdoa_in

def test_epoch(data_generator, model, criterion, dcase_output_folder, params, device):
    # Number of frames for a 60 second audio with 100ms hop length = 600 frames
    # Number of frames in one batch (batch_size* sequence_length) consists of all the 600 frames above with zero padding in the remaining frames
    max_frames_with_content = data_generator.get_nb_frames()
    frames_per_file = data_generator.get_frame_per_file()
    test_filelist = data_generator.get_filelist()

    nb_test_batches, test_loss = 0, 0.
    model.eval()
    file_cnt = 0
    with torch.no_grad():
        for data, target in data_generator.generate():
            # load one batch of data
            data, target = torch.tensor(data).to(device).float(), torch.tensor(target).to(device).float()

            # process the batch of data based on chosen mode
            output = model(data)
            loss = criterion(output, target)
            sed_pred, doa_pred = get_accdoa_labels(output.detach().cpu().numpy(), params['unique_classes'])
            sed_pred = reshape_3Dto2D(sed_pred)
            doa_pred = reshape_3Dto2D(doa_pred)

            # dump SELD results to the correspondin file
            output_file = os.path.join(dcase_output_folder, test_filelist[file_cnt].replace('.npy', '.csv'))
            file_cnt += 1
            output_dict = {}
            for frame_cnt in range(sed_pred.shape[0]):
                for class_cnt in range(sed_pred.shape[1]):
                    if sed_pred[frame_cnt][class_cnt]>0.5:
                        if frame_cnt not in output_dict:
                            output_dict[frame_cnt] = []
                        output_dict[frame_cnt].append([class_cnt, doa_pred[frame_cnt][class_cnt], doa_pred[frame_cnt][class_cnt+params['unique_classes']], doa_pred[frame_cnt][class_cnt+2*params['unique_classes']]]) 

            data_generator.write_output_format_file(output_file, output_dict)

            test_loss += loss.item()
            nb_test_batches += 1
            if params['quick_test'] and nb_test_batches == 4:
                break


        test_loss /= nb_test_batches

    return test_loss


def train_epoch(data_generator, optimizer, model, criterion, params, device):
    nb_train_batches, train_loss = 0, 0.
    model.train()
    for data, target in data_generator.generate():
        # load one batch of data
        data, target = torch.tensor(data).to(device).float(), torch.tensor(target).to(device).float()
        optimizer.zero_grad()

        # process the batch of data based on chosen mode
        output = model(data)
       
        loss = criterion(output, target)
        loss.backward()
        optimizer.step()
        
        train_loss += loss.item()
        nb_train_batches += 1
        if params['quick_test'] and nb_train_batches == 4:
            break

    train_loss /= nb_train_batches

    return train_loss


def main(argv):
    """
    Main wrapper for training sound event localization and detection network.

    :param argv: expects two optional inputs.
        first input: task_id - (optional) To chose the system configuration in parameters.py.
                                (default) 1 - uses default parameters
        second input: job_id - (optional) all the output files will be uniquely represented with this.
                              (default) 1

    """
    print(argv)
    if len(argv) != 3:
        print('\n\n')
        print('-------------------------------------------------------------------------------------------------------')
        print('The code expected two optional inputs')
        print('\t>> python seld.py <task-id> <job-id>')
        print('\t\t<task-id> is used to choose the user-defined parameter set from parameter.py')
        print('Using default inputs for now')
        print('\t\t<job-id> is a unique identifier which is used for output filenames (models, training plots). '
              'You can use any number or string for this.')
        print('-------------------------------------------------------------------------------------------------------')
        print('\n\n')

    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")
    torch.autograd.set_detect_anomaly(True)

    # use parameter set defined by user
    task_id = '1' if len(argv) < 2 else argv[1]
    params = parameters.get_params(task_id)

    job_id = 1 if len(argv) < 3 else argv[-1]

    # Training setup
    train_splits, val_splits, test_splits = None, None, None
    if params['mode'] == 'dev':
        if '2020' in params['dataset_dir']:
            test_splits = [1]
            val_splits = [2]
            train_splits = [[3, 4, 5, 6]]

        elif '2021' in params['dataset_dir']:
            test_splits = [6]
            val_splits = [5]
            train_splits = [[1, 2, 3, 4]]
        else:
            print('ERROR: Unknown dataset splits')
            exit()
    for split_cnt, split in enumerate(test_splits):
        print('\n\n---------------------------------------------------------------------------------------------------')
        print('------------------------------------      SPLIT {}   -----------------------------------------------'.format(split))
        print('---------------------------------------------------------------------------------------------------')

        # Unique name for the run
        cls_feature_class.create_folder(params['model_dir'])
        unique_name = '{}_{}_{}_{}_split{}'.format(
            task_id, job_id, params['dataset'], params['mode'], split
        )
        unique_name = os.path.join(params['model_dir'], unique_name)
        model_name = '{}_model.h5'.format(unique_name)
        print("unique_name: {}\n".format(unique_name))

        # Load train and validation data
        print('Loading training dataset:')
        data_gen_train = cls_data_generator.DataGenerator(
            params=params, split=train_splits[split_cnt]
        )

        print('Loading validation dataset:')
        data_gen_val = cls_data_generator.DataGenerator(
            params=params, split=val_splits[split_cnt], shuffle=False, per_file=True
        )

        # Collect i/o data size and load model configuration
        data_in, data_out = data_gen_train.get_data_sizes()
        model = seldnet_model.CRNN(data_in, data_out, params).to(device)
        #model.load_state_dict(torch.load("models/11_7862293_foa_dev_split6_model.h5", map_location='cpu'))

        print('---------------- DOA-net -------------------')
        print('FEATURES:\n\tdata_in: {}\n\tdata_out: {}\n'.format(data_in, data_out))
        print('MODEL:\n\tdropout_rate: {}\n\tCNN: nb_cnn_filt: {}, f_pool_size{}, t_pool_size{}\n\trnn_size: {}, fnn_size: {}\n'.format(
            params['dropout_rate'], params['nb_cnn2d_filt'], params['f_pool_size'], params['t_pool_size'], params['rnn_size'],
            params['fnn_size']))
        print(model)

        # Dump results in DCASE output format for calculating final scores
        dcase_output_val_folder = os.path.join(params['dcase_output_dir'], '{}_{}_{}_{}_val'.format(task_id, params['dataset'], params['mode'], strftime("%Y%m%d%H%M%S", gmtime())))
        cls_feature_class.delete_and_create_folder(dcase_output_val_folder)
        print('Dumping recording-wise val results in: {}'.format(dcase_output_val_folder))

        # Initialize evaluation metric class
        score_obj = ComputeSELDResults(params)

        # start training
        best_val_epoch = -1
        best_ER, best_F, best_LE, best_LR, best_seld_scr = 1., 0., 180., 0., 9999 
        patience_cnt = 0

        nb_epoch = 2 if params['quick_test'] else params['nb_epochs']
        optimizer = optim.Adam(model.parameters(), lr=params['lr'])
        criterion = nn.MSELoss()

        for epoch_cnt in range(nb_epoch):
            # ---------------------------------------------------------------------
            # TRAINING
            # ---------------------------------------------------------------------
            start_time = time.time()
            train_loss = train_epoch(data_gen_train, optimizer, model, criterion, params, device)
            train_time = time.time() - start_time

            # ---------------------------------------------------------------------
            # VALIDATION
            # ---------------------------------------------------------------------
            start_time = time.time()
            val_loss = test_epoch(data_gen_val, model, criterion, dcase_output_val_folder, params, device)

            # Calculate the DCASE 2021 metrics - Location-aware detection and Class-aware localization scores
            val_ER, val_F, val_LE, val_LR, val_seld_scr = score_obj.get_SELD_Results(dcase_output_val_folder)

            val_time = time.time() - start_time

            # Save model if loss is good
            if val_seld_scr <= best_seld_scr:
                best_val_epoch, best_ER, best_F, best_LE, best_LR, best_seld_scr = epoch_cnt, val_ER, val_F, val_LE, val_LR, val_seld_scr
                torch.save(model.state_dict(), model_name)

            # Print stats
            print(
                'epoch: {}, time: {:0.2f}/{:0.2f}, '
                'train_loss: {:0.2f}, val_loss: {:0.2f}, '
                'ER/F/LE/LR/SELD: {}, '
                'best_val_epoch: {} {}'.format(
                    epoch_cnt, train_time, val_time,
                    train_loss, val_loss,
                    '{:0.2f}/{:0.2f}/{:0.2f}/{:0.2f}/{:0.2f}'.format(val_ER, val_F, val_LE, val_LR, val_seld_scr),
                    best_val_epoch, '({:0.2f}/{:0.2f}/{:0.2f}/{:0.2f}/{:0.2f})'.format(best_ER, best_F, best_LE, best_LR, best_seld_scr))
            )

            patience_cnt += 1
            if patience_cnt > params['patience']:
                break

        # ---------------------------------------------------------------------
        # Evaluate on unseen test data
        # ---------------------------------------------------------------------
        print('Load best model weights')
        model.load_state_dict(torch.load(model_name, map_location='cpu'))

        print('Loading unseen test dataset:')
        data_gen_test = cls_data_generator.DataGenerator(
            params=params, split=test_splits[split_cnt], shuffle=False, per_file=True
        )

        # Dump results in DCASE output format for calculating final scores
        dcase_output_test_folder = os.path.join(params['dcase_output_dir'], '{}_{}_{}_test'.format(task_id, params['dataset'], params['mode']))
        cls_feature_class.delete_and_create_folder(dcase_output_test_folder)
        print('Dumping recording-wise test results in: {}'.format(dcase_output_test_folder))


        test_loss = test_epoch(data_gen_test, model, criterion, dcase_output_test_folder, params, device)

        test_ER, test_F, test_LE, test_LR, test_seld_scr = score_obj.get_SELD_Results(dcase_output_test_folder)

        print(
            'test_loss: {:0.2f}, ER/F/LE/LR/SELD: {}'.format(
                test_loss,
                '{:0.2f}/{:0.2f}/{:0.2f}/{:0.2f}/{:0.2f}'.format(test_ER, test_F, test_LE, test_LR, test_seld_scr))
        )


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (ValueError, IOError) as e:
        sys.exit(e)

