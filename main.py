
from options import args
import random
import numpy as np
import torch
import csv
import sys
from utils import load_lookups, prepare_instance, prepare_instance_bert, MyDataset, my_collate, my_collate_bert, \
    early_stop, save_everything, prepare_instance_tf, my_collate_tf, prepare_instance_entity, my_collate_entity, load_lookups_lv0, load_lookups_lv1, load_lookups_hybrid, \
    prepare_instance_hybrid, my_collate_hybrid
from learn.models_new import pick_model
import torch.optim as optim
from collections import defaultdict
from torch.utils.data import DataLoader
import os
import time
from train_test import train, test

if __name__ == "__main__":


    if args.random_seed != 0:
        random.seed(args.random_seed)
        np.random.seed(args.random_seed)
        torch.manual_seed(args.random_seed)
        torch.cuda.manual_seed_all(args.random_seed)

    print(args)

    maxInt = sys.maxsize
    while True:
        # decrease the maxInt value by factor 10
        # as long as the OverflowError occurs.

        try:
            csv.field_size_limit(maxInt)
            break
        except OverflowError:
            maxInt = int(maxInt / 10)


    # load vocab and other lookups
    print("loading lookups...")
    if args.pre_level == 'lv0':
        dicts = load_lookups_lv0(args)
    elif args.pre_level == 'lv1':
        dicts = load_lookups_lv1(args)
    elif args.pre_level == 'hybrid':
        dicts = load_lookups_hybrid(args)
    else:
        dicts = load_lookups(args)

    model = pick_model(args, dicts)
    print(model)

    if not args.test_model:
        optimizer = optim.Adam(model.parameters(), weight_decay=args.weight_decay, lr=args.lr)
    else:
        optimizer = None

    if args.tune_wordemb == False:
        model.freeze_net()

    metrics_hist = defaultdict(lambda: [])
    metrics_hist_te = defaultdict(lambda: [])
    metrics_hist_tr = defaultdict(lambda: [])

    if args.model.find("bert") != -1:
        prepare_instance_func = prepare_instance_bert
    elif args.model.find("EntityEH") != -1 or args.model.find("EntityFlowHidden") != -1:
        prepare_instance_func = prepare_instance_tf
    elif args.model.find("EntityFlow") != -1:
        prepare_instance_func = prepare_instance_entity
    elif args.model.find("Hybrid") != -1:
        prepare_instance_func = prepare_instance_hybrid
    else:
        prepare_instance_func = prepare_instance

    train_instances = prepare_instance_func(dicts, args.data_path, args, args.MAX_LENGTH)
    print("train_instances {}".format(len(train_instances)))
    if args.version != 'mimic2':
        dev_instances = prepare_instance_func(dicts, args.data_path.replace('train','dev').replace('full', args.Y), args, args.MAX_LENGTH)
        print("dev_instances {}".format(len(dev_instances)))
    else:
        dev_instances = None
    test_instances = prepare_instance_func(dicts, args.data_path.replace('train','test').replace('full', args.Y), args, args.MAX_LENGTH)
    print("test_instances {}".format(len(test_instances)))

    if args.model.find("bert") != -1:
        collate_func = my_collate_bert
    elif args.model.find("EntityEH") != -1 or args.model.find("EntityFlowHidden") != -1:
        collate_func = my_collate_tf
    elif args.model.find("EntityFlow") != -1:
        collate_func = my_collate_entity
    elif args.model.find("Hybrid") != -1:
        collate_func = my_collate_hybrid
    else:
        collate_func = my_collate

    train_loader = DataLoader(MyDataset(train_instances), args.batch_size, shuffle=True, collate_fn=collate_func)
    if args.version != 'mimic2':
        dev_loader = DataLoader(MyDataset(dev_instances), 1, shuffle=False, collate_fn=collate_func)
    else:
        dev_loader = None
    test_loader = DataLoader(MyDataset(test_instances), 1, shuffle=False, collate_fn=collate_func)

    test_only = args.test_model is not None

    for epoch in range(args.n_epochs):

        if epoch == 0 and not args.test_model:
            model_dir = os.path.join(args.MODEL_DIR, args.model)
            if not os.path.exists(model_dir):
                os.makedirs(model_dir)
        elif args.test_model:
            model_dir = os.path.dirname(os.path.abspath(args.test_model))

        if not test_only:
            epoch_start = time.time()
            losses = train(args, model, optimizer, epoch, args.gpu, train_loader, dicts)
            loss = np.mean(losses)
            epoch_finish = time.time()
            print("epoch finish in %.2fs, loss: %.4f" % (epoch_finish - epoch_start, loss))
        else:
            loss = np.nan

        fold = 'test' if args.version == 'mimic2' else 'dev'
        dev_instances = test_instances if args.version == 'mimic2' else dev_instances
        dev_loader = test_loader if args.version == 'mimic2' else dev_loader
        if epoch == args.n_epochs - 1:
            print("last epoch: testing on dev and test sets")
            test_only = True

        # test on dev
        evaluation_start = time.time()
        metrics = test(args, model, args.data_path.replace('full', args.Y), fold, args.gpu, dicts, dev_loader)
        metrics_te = test(args, model, args.data_path.replace('full', args.Y), 'test', args.gpu, dicts, test_loader)
        evaluation_finish = time.time()
        print("evaluation finish in %.2fs" % (evaluation_finish - evaluation_start))
        if test_only or epoch == args.n_epochs - 1:
            args.test_model = '%s/model_best_%s.pth' % (model_dir, args.criterion)
            model = pick_model(args, dicts)
            metrics_te = test(args, model, args.data_path.replace('full', args.Y), "test", args.gpu, dicts, test_loader)
        else:
            metrics_te = defaultdict(float)
        metrics_tr = {'loss': loss}
        metrics_all = (metrics, metrics_te, metrics_tr)

        for name in metrics_all[0].keys():
            metrics_hist[name].append(metrics_all[0][name])
        for name in metrics_all[1].keys():
            metrics_hist_te[name].append(metrics_all[1][name])
        for name in metrics_all[2].keys():
            metrics_hist_tr[name].append(metrics_all[2][name])
        metrics_hist_all = (metrics_hist, metrics_hist_te, metrics_hist_tr)

        save_everything(args, metrics_hist_all, model, model_dir, None, args.criterion, test_only)

        sys.stdout.flush()

        if test_only:
            break

        if args.criterion in metrics_hist.keys():
            if early_stop(metrics_hist, args.criterion, args.patience):
                #stop training, do tests on test and train sets, and then stop the script
                print("%s hasn't improved in %d epochs, early stopping..." % (args.criterion, args.patience))
                test_only = True
                args.test_model = '%s/model_best_%s.pth' % (model_dir, args.criterion)
                model = pick_model(args, dicts)



