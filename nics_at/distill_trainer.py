# -*- coding: utf-8 -*-
from __future__ import print_function

import os
import time
import sys
from datetime import datetime
from collections import OrderedDict

import numpy as np
import tensorflow as tf

from models import QCNN, QCNNProxy
import utils
from utils import AvailModels, LrAdjuster
from attacks import Attack, AttackGenerator
from base_trainer import settings, Trainer

class DistillTrainer(Trainer):
    class _settings(settings):
        default_cfg = {
            "model": None,
            "test_frequency": 1,

            # Data gen
            "dataset": "tinyimagenet",
            "dataset_info": {},
            "num_threads": 2,
            "capacity": 1024,
            "more_augs": False,
            # only use when using subclass of GrayDataset
            "gray_dataset_device": 1,
            "sync_every": 5,
            "additional_models_gray": [],
            "available_attacks_gray": [],

            # Training
            "optimizer": {"type": "Momentum", "args": {"momentum": 0.9}},
            "additional_info_attrs": ["distillation"],
            "gradient_smooth_reg": 0,
            "gradient_smooth_reg_type": None,
            "gradient_norm_reg": 0,
            "gradient_norm_reg_ord": 1,
            "multiple_head_loss": False,
            "use_mixup": False,
            "mixup_alpha": 1.0,
            "distill_use_auged": False, # 一个谜一样的bug
            "epochs": 50,
            "batch_size": 100,
            "adjust_lr_acc": None,

            "alpha": 0.1,
            "beta": 0,
            "theta": 0.5,
            "temperature": 1,
            "at_mode": "attention",
            "train_models": {},
            "update_per_batch": 1, # this configuration is deprecating...
            "distill_self": False,
            "distill_loss_type": "crossentropy",
            "relu_thresh_schedule": None,

            # Testing
            "test_saltpepper": None,
            "test_models": {},

            # Augmentaion
            "aug_saltpepper": None,
            "aug_gaussian": None,

            # Adversarial Augmentation
            "available_attacks": [],
            "generated_adv": [],
            "train_merge_adv": False,
            "split_adv": False,
            "test_split_adv": False,
            "multi_grad_accumulate": False,
            "random_split_adv": False,
            "random_interp": None,
            "random_interp_adv": None,
            "test_random_interp_adv": None,

            "additional_models": []
        }
    def __init__(self, args, cfg):
        super(DistillTrainer, self).__init__(args, cfg)
        assert self.FLAGS.distill_loss_type in {"crossentropy", "gaussian", "L2"}

    def init(self):
        # batch_size = self.FLAGS.batch_size # default to 128
        self.num_labels = self.dataset.num_labels

        (self.imgs_t, self.auged_imgs_t, self.labels_t, self.adv_imgs_t), (self.imgs_v, self.auged_imgs_v, self.labels_v, self.adv_imgs_v) = self.dataset.data_tensors
        utils.log("Train number: {}; Validation number: {}".format(self.dataset.train_num, self.dataset.val_num))

        self.x = tf.placeholder(tf.float32, shape=[None] + list(self.dataset.image_shape), name="x")
        self.stu_x = tf.placeholder(tf.float32, shape=[None] + list(self.dataset.image_shape), name="stu_x")
        self.labels = tf.placeholder(tf.float32, [None, self.dataset.num_labels], name="labels")

        self.model_stu = QCNN.create_model(self.FLAGS["model"])
        self.logits_stu = self.model_stu.get_logits(self.stu_x)
        AvailModels.add(self.model_stu, self.stu_x, self.logits_stu)
        if self.FLAGS.use_denoiser:
            AvailModels.add(self.model_stu.inner_model, self.model_stu.denoiser.denoise_output, self.logits_stu)

        # additional test only models
        self.additional_models = []
        for i in range(len(self.FLAGS["additional_models"])):
            m_cfg = self.FLAGS["additional_models"][i]
            x = tf.placeholder(tf.float32, shape=[None] + self.dataset.image_shape, name="x_addi_{}".format(i))
            model = QCNN.create_model(m_cfg)
            logits = model.get_logits(x)
            AvailModels.add(model, x, logits)
            self.additional_models.append(model)

        if self.FLAGS.alpha != 0: # distill
            if not self.FLAGS.distill_self:
                self.model_tea = QCNN.create_model(self.FLAGS["teacher"])
                self.logits = self.model_tea.get_logits(self.x)
                AvailModels.add(self.model_tea, self.x, self.logits)
            else:
                self.model_tea = self.model_stu
                self.logits = tf.stop_gradient(self.model_tea.get_logits(self.x))

        trainable_variables = self.model_stu.trainable_vars
        tf.get_default_graph().clear_collection("trainable_variables")
        for var in trainable_variables:
            tf.add_to_collection("trainable_variables", var)

        self.training_stu = self.model_stu.get_training_status()

        # Loss and metrics
        # tile_num = tf.shape(self.logits_stu)[0]/batch_size
        tile_num = tf.shape(self.logits_stu)[0]/tf.shape(self.labels)[0]
        if self.FLAGS.alpha != 0:
            tile_num_tea = tf.shape(self.logits)[0]/tf.shape(self.labels)[0] # note teacher input batch size must be larger than label batch size

            soft_label = tf.nn.softmax(self.logits/self.FLAGS.temperature)
            soft_logits = self.logits_stu / self.FLAGS.temperature
            reshape_soft_label = tf.reshape(tf.tile(tf.expand_dims(soft_label, 1), [1, tf.shape(soft_logits)[0]/tf.shape(soft_label)[0], 1]), [-1, self.num_labels])
            if self.FLAGS.distill_loss_type == "gaussian":
                ce = tf.reduce_sum((tf.nn.softmax(reshape_soft_label) - tf.nn.softmax(soft_logits))**2, axis=-1)
            elif self.FLAGS.distill_loss_type == "L2":
                ce = tf.reduce_mean((reshape_soft_label - soft_logits)**2, axis=-1)
            else:
                ce = tf.nn.softmax_cross_entropy_with_logits(
                    labels=reshape_soft_label,
                    logits=soft_logits,
                    name="distill_ce_loss")
            self.distillation = tf.reduce_mean(ce)
        else:
            self.distillation = tf.constant(0.0)

        reshape_labels = tf.reshape(tf.tile(tf.expand_dims(self.labels, 1), [1, tile_num, 1]), [-1, self.num_labels])
        if not self.FLAGS.multiple_head_loss:
            self.original_loss = tf.reduce_mean(
                tf.nn.softmax_cross_entropy_with_logits(labels=reshape_labels, logits=self.logits_stu))
        else:
            def get_ith_group_logits(i):
                def _get_logits(self_, inputs_):
                    return self.model_stu.get_logits(inputs_, output_name="group_logits_list")[i]
                return _get_logits
            for i, logits in enumerate(self.model_stu.cached[self.stu_x]["group_logits_list"]):
                model = QCNNProxy(self.model_stu, get_ith_group_logits(i))
                AvailModels.add(model, self.stu_x, logits, name="{}_group_head_{}".format(self.model_stu.namescope, i))
            self.original_loss = tf.reduce_mean([self.FLAGS.multiple_head_loss[i] * tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(labels=reshape_labels, logits=logits)) for i, logits in enumerate(self.model_stu.cached[self.stu_x]["group_logits_list"] + [self.logits_stu])])

        self.loss = self.original_loss * self.FLAGS.theta
        if self.FLAGS.gradient_smooth_reg or self.FLAGS.gradient_norm_reg: 
            self.input_gradient = tf.gradients(self.original_loss * self.FLAGS.theta, self.stu_x)[0]
        if self.FLAGS.gradient_smooth_reg:
            # input gradient smoothness of crossentropy loss
            vert_grad_diff = self.input_gradient[:, :-1, :, :] - self.input_gradient[:, 1:, :, :]
            hori_grad_diff = self.input_gradient[:, :, :-1, :] - self.input_gradient[:, :, 1:, :]
            grad_local_diff = tf.reduce_sum(tf.reduce_mean(vert_grad_diff ** 2, axis=0)) + tf.reduce_sum(tf.reduce_mean(hori_grad_diff ** 2, axis=0))
            grad_smooth_coeff = self.FLAGS.gradient_smooth_reg
            if self.FLAGS.gradient_smooth_reg_type == "loss_match":
                grad_smooth_coeff = grad_smooth_coeff * self.loss / (grad_local_diff + 1e-8)
            elif self.FLAGS.gradient_smooth_reg_type == "grad_match":
                assert Exception("Not implemented now")
            self.grad_smooth_loss = grad_smooth_coeff * grad_local_diff
            self.loss += self.grad_smooth_loss
        else:
            self.grad_smooth_loss = tf.constant(0.0)

        if self.FLAGS.gradient_norm_reg:
            if self.FLAGS.gradient_norm_reg_ord == 1:
                self.grad_norm_loss = self.FLAGS.gradient_norm_reg * tf.reduce_sum(tf.reduce_mean(tf.abs(self.input_gradient), axis=0))
            elif self.FLAGS.gradient_norm_reg_ord == 2:
                self.grad_norm_loss = self.FLAGS.gradient_norm_reg * tf.reduce_sum(tf.reduce_mean(tf.square(self.input_gradient), axis=0))
            else:
                raise Exception("gradient_norm_reg_ord must be in {1, 2}")
            self.loss += self.grad_norm_loss
        else:
            self.grad_norm_loss = tf.constant(0.0)

        if self.FLAGS.alpha != 0:
            self.loss += self.distillation * self.FLAGS.alpha
        if self.FLAGS.beta != 0:
            pass # not implemented, as we found this not very effective in initial exps
            # self.at_loss = get_at_loss(group_list_teacher, group_list_student)
            # self.loss += at_loss * self.FLAGS.beta
        else:
            self.at_loss = tf.constant(0.0)
        # Add regularization loss
        self.loss += tf.losses.get_regularization_loss()

        self.index_label = tf.argmax(self.labels, -1)
        _tmp = tf.expand_dims(self.index_label, -1)
        reshape_index_label = tf.reshape(tf.tile(_tmp, [1, tile_num]), [-1])
        correct = tf.equal(tf.argmax(self.logits_stu, -1), reshape_index_label)
        self.accuracy = tf.reduce_mean(tf.cast(correct, tf.float32))
        if self.FLAGS.alpha != 0:
            reshape_index_label_tea = tf.reshape(tf.tile(_tmp, [1, tile_num_tea]), [-1])
            tea_correct = tf.equal(tf.argmax(self.logits, -1), reshape_index_label_tea)
            self.tea_accuracy = tf.reduce_mean(tf.cast(tea_correct, tf.float32))
        else:
            self.tea_accuracy = self.accuracy

        # Initialize the optimizer
        self.learning_rate = tf.placeholder(tf.float32, shape=[])
        self.lr_adjuster = LrAdjuster.create_adjuster(self.FLAGS.adjust_lr_acc)
        # By default: optimizer = tf.train.MomentumOptimizer(self.learning_rate, momentum=0.9)
        optimizer = getattr(tf.train, self.FLAGS.optimizer["type"].capitalize() + "Optimizer")(self.learning_rate, **self.FLAGS.optimizer["args"])
        # if not self.FLAGS.use_denoiser:
        #     update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS, self.FLAGS.model["namescope"]) # NOTE: student must have a non-empty namescope
        # else:
        #     update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS, self.FLAGS.model["namescope"] + "/" + self.FLAGS.model["model_params"]["denoiser"]["namescope"]) # NOTE: student must have a non-empty namescope
        update_ops = self.model_stu.update_ops

        if self.FLAGS.multi_grad_accumulate:
            tvs = tf.trainable_variables()
            accum_vars = [tf.Variable(tf.zeros_like(tv), trainable=False) for tv in tvs]
            self.zero_agrad_op = [tv.assign(tf.zeros_like(tv)) for tv in accum_vars]
            self.grads_and_vars = optimizer.compute_gradients(self.loss, tvs)
            # NOTE: the batch norm update is done every small iter (hope it will not cause severe vibration)
            with tf.control_dependencies(update_ops):
                self.accum_ops = [accum_vars[i].assign_add(gv[0]) for i, gv in enumerate(self.grads_and_vars)]
            self.train_step = optimizer.apply_gradients([(accum_vars[i], gv[1]) for i, gv in enumerate(self.grads_and_vars)])
        else:
            with tf.control_dependencies(update_ops):
                self.grads_and_var = optimizer.compute_gradients(self.loss)
                self.train_step = optimizer.apply_gradients(self.grads_and_var)

        # Initialize relu thrshold schedule adjuster
        if self.FLAGS.relu_thresh_schedule is not None:
            self.relu_thresh_adjuster = LrAdjuster.create_adjuster(self.FLAGS.relu_thresh_schedule, name="relu_thresh")

        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        self.sess = tf.Session(config=config)
        [Attack.create_attack(self.sess, a_cfg) for a_cfg in (self.FLAGS["available_attacks"] or [])]
        self.train_attack_gen = AttackGenerator(self.FLAGS["train_models"], merge=self.FLAGS.train_merge_adv, split_adv=self.FLAGS.split_adv, random_split_adv=self.FLAGS.random_split_adv,
                                                random_interp=self.FLAGS.random_interp, random_interp_adv=self.FLAGS.random_interp_adv, mixup_alpha=self.FLAGS.mixup_alpha, name="train")
        self.test_attack_gen = AttackGenerator(self.FLAGS["test_models"], split_adv=self.FLAGS.test_split_adv, random_interp_adv=self.FLAGS.test_random_interp_adv, name="test")

    def train(self):
        sess = self.sess
        steps_per_epoch = self.dataset.train_num // self.FLAGS.batch_size
        if self.FLAGS.relu_thresh_schedule is not None:
            relu_thresh_v = self.FLAGS.relu_thresh_schedule["start_lr"]
        for epoch in range(1, self.FLAGS.epochs+1):
            self.train_attack_gen.new_epoch()
            start_time = time.time()
            info_v_epoch = np.zeros((self.FLAGS.update_per_batch, len(self.info_attrs)))
            now_lr = self.lr_adjuster.get_lr()
            if now_lr is None:
                utils.log("End training as val acc not decay!!!")
                return
            else:
                utils.log("Lr: ", now_lr)

            if self.FLAGS.relu_thresh_schedule is not None:
                new_relu_thresh_v = self.relu_thresh_adjuster.get_lr()
                sess.run(tf.assign(self.model_stu.relu_thresh, relu_thresh_v)) # assign new relu_thresh_v when every train epoch start
                if not new_relu_thresh_v == relu_thresh_v:
                    accs_after_adjust = self.test(adv=True, name="test ajudst relu_thresh to {}".format(new_relu_thresh_v))
                    self.relu_thresh_adjuster.set_status(best_acc=accs_after_adjust, best_epoch=epoch-1)
                relu_thresh_v = new_relu_thresh_v

            # Train batches
            gen_time = 0
            run_time = 0
            fetch_time = 0
            for step in range(1, steps_per_epoch+1):
                self.train_attack_gen.new_batch()

                fetch_start_time = time.time()
                x_v, auged_x_v, y_v, adv_x_v = sess.run([self.imgs_t, self.auged_imgs_t, self.labels_t, self.adv_imgs_t])
                fetch_time += time.time() - fetch_start_time

                gen_start_time = time.time()
                _, adv_xs, ys = self.train_attack_gen.generate_for_model(auged_x_v, y_v, self.FLAGS.model["namescope"], adv_x_v)
                gen_time += time.time() - gen_start_time
                inner_info_v = []
                run_start_time = time.time()
                if step == 1 and info_v_epoch.shape[0] != len(adv_xs):
                    info_v_epoch = np.zeros((len(adv_xs), len(info_attrs)))
                actual_lr = now_lr / len(adv_xs)
                if self.FLAGS.multi_grad_accumulate:
                    sess.run(self.zero_agrad_op)
                for adv_x, s_y in zip(adv_xs, ys):
                    feed_dict = {
                        self.x: x_v if not self.FLAGS.distill_use_auged else auged_x_v,
                        self.stu_x: adv_x,
                        self.training_stu: True,
                        # self.labels: y_v,
                        # self.labels: s_y, # only use this for mixup
                        self.labels: s_y if self.FLAGS.use_mixup else y_v,
                        self.learning_rate: actual_lr
                    }
                    if not self.FLAGS.multi_grad_accumulate:
                        info_v, _ = sess.run([self.info_attrs, self.train_step], feed_dict=feed_dict)
                    else:
                        info_v, _ = sess.run([self.info_attrs, self.accum_ops], feed_dict=feed_dict)
                    inner_info_v.append(info_v)
                if self.FLAGS.multi_grad_accumulate:
                    sess.run(self.train_step, feed_dict={self.learning_rate: actual_lr})
                run_time += time.time() - run_start_time
                info_v_epoch += inner_info_v
                if step % self.FLAGS.print_every == 0:
                    print(("\rEpoch {}: steps {}/{} loss: {} additional: " + "/".join([str(n) + ":{}" for n in self.FLAGS.additional_info_attrs]))
                          .format(epoch, step, steps_per_epoch, *np.mean(inner_info_v, axis=0)[2:]), end="")
            gen_time = gen_time / steps_per_epoch
            run_time = run_time / steps_per_epoch
            fetch_time = fetch_time / steps_per_epoch
            info_v_epoch /= steps_per_epoch
            duration = time.time() - start_time
            sec_per_batch = duration / steps_per_epoch
            print_info = np.mean(info_v_epoch, axis=0)
            acc_stu_epoch, acc_tea_epoch, loss_v_epoch = print_info[:3]
            addi_info = "; ".join(["{k}: {v}".format(k=k, v=v) for k, v in zip(self.FLAGS.additional_info_attrs, print_info[3:])])
            utils.log(("\r{}: Epoch {}; (average) loss: {:.3f}; (average) student accuracy: {:.2f} %; (average) teacher accuracy: {:.2f} %."
                       " {:.3f} sec/batch; gen time: {:.3f} sec/batch; run time: {:.3f} sec/batch; fetch time: {:.3f} sec/batch; {}\n\tadditional info: {};")
                      .format(datetime.now(), epoch, loss_v_epoch, acc_stu_epoch * 100, acc_tea_epoch * 100, sec_per_batch, gen_time, run_time, fetch_time,
                              "" if not utils.PROFILING else "; ".join(["{}: {:.2f} ({:.3f} average) sec".format(k, t, t/num) for k, (num, t) in utils.all_profiled.iteritems()]),
                              addi_info
                          ), flush=True)
            # End training batches

            # Test on the validation set
            if epoch % self.FLAGS.test_frequency == 0:
                test_accs = self.test(adv=True, name="normal_adv")
                is_best = self.lr_adjuster.add_multiple_acc(test_accs)
                if self.FLAGS.relu_thresh_schedule is not None:
                    self.relu_thresh_adjuster.add_multiple_acc(test_accs)
                if self.FLAGS.train_dir:
                    if is_best or (self.FLAGS.save_every > 0 and epoch % self.FLAGS.save_every == 0):
                        save_path = os.path.join(self.FLAGS.train_dir, str(epoch))
                        self.model_stu.save_checkpoint(save_path, sess)
                        utils.log("Saved student model to: ", save_path)
                self.dataset.sync_epoch(epoch)

    def test(self, saltpepper=None, adv=False, name=""):
        sess = self.sess
        steps_per_epoch = self.dataset.val_num // self.FLAGS.batch_size
        loss_v_epoch = 0
        acc_v_epoch = 0
        tea_acc_v_epoch = 0
        image_disturb = 0
        test_res = OrderedDict()
        for step in range(1, steps_per_epoch+1):
            self.test_attack_gen.new_batch()
            x_v, auged_x_v, y_v, adv_x_v = sess.run([self.imgs_v, self.auged_imgs_v, self.labels_v, self.adv_imgs_v])
            print("\rTesting {}/{}".format(step, steps_per_epoch), end="")
            if saltpepper is not None: # during test, saltpepper is added at last, this is a train-test discrepancy, but i don't think it matters
                img = x_v
                u = np.random.uniform(size=list(x_v.shape[:3]) + [1])
                salt = (u >= 1 - saltpepper/2).astype(x_v.dtype) * 256
                pepper = - (u < saltpepper/2).astype(x_v.dtype) * 256
                img = np.clip(img + salt + pepper, 0, 255)
                auged_x = img
            else:
                auged_x = x_v
            acc_v, tea_acc_v, loss_v = sess.run([self.accuracy, self.tea_accuracy, self.original_loss], feed_dict={
                self.x: auged_x,
                self.stu_x: auged_x,
                self.labels: y_v,
                self.training_stu: False
            })
            image_disturb += np.abs(auged_x - x_v).mean()
            loss_v_epoch += loss_v
            acc_v_epoch += acc_v
            tea_acc_v_epoch += tea_acc_v
            # test adv
            if adv:
                test_ids, adv_xs, _ = self.test_attack_gen.generate_for_model(auged_x_v, y_v, "stu_", adv_x_v)
                for test_id, adv_x in zip(test_ids, adv_xs):
                    acc_v, tea_acc_v, loss_v = sess.run([self.accuracy, self.tea_accuracy, self.original_loss], feed_dict={
                        self.stu_x: adv_x,
                        self.x: adv_x,
                        self.labels: y_v,
                        self.training_stu: False
                    })
                    if test_id not in test_res:
                        test_res[test_id] = np.zeros(4)
                    if adv_x.shape != auged_x_v.shape:
                        sp = [auged_x_v.shape[0], adv_x.shape[0] / auged_x_v.shape[0]] + list(auged_x_v.shape[1:])
                        tmp_adv_x = adv_x.reshape(sp)
                        sp[1] = 1
                        mean_dist = np.mean(np.abs(tmp_adv_x - auged_x_v.reshape(sp)))
                    else:
                        mean_dist = np.mean(np.abs(adv_x - auged_x_v)) # L1 dist
                    test_res[test_id] += [acc_v, tea_acc_v, loss_v, mean_dist]
        image_disturb /= steps_per_epoch
        loss_v_epoch /= steps_per_epoch
        acc_v_epoch /= steps_per_epoch
        tea_acc_v_epoch /= steps_per_epoch
        print("\r", end="")
        utils.log("\tTest {}: \n\t\tloss: {}; accuracy: {:.2f} %; teacher accuracy: {:.2f} %; Mean pixel distance: {:.2f}".format(name, loss_v_epoch, acc_v_epoch * 100, tea_acc_v_epoch * 100, image_disturb))
        if adv:
            utils.log("\tAdv:\n\t\t{}".format("\n\t\t".join(["test {}: acc: {:.3f}; tea_acc: {:.3f}; ce_loss: {:.2f}; dist: {:.2f}".format(test_id, *(attack_res/steps_per_epoch)) for test_id, attack_res in test_res.items()])), flush=True)
        return [acc_v_epoch] + [v[0]/steps_per_epoch for v in test_res.values()]

    def start(self):
        sess = self.sess
        if self.FLAGS.train_dir:
            train_writer = tf.summary.FileWriter(self.FLAGS.train_dir + '/train',
                                                 sess.graph)
        sess.run(tf.group(tf.global_variables_initializer(), tf.local_variables_initializer()))

        if self.FLAGS.test_only:
            if not (self.FLAGS.load_file_stu or self.FLAGS.load_file_tea):
                print("error: no input file. Must supply teacher model or stu model when testing.")
                sys.exit(1)

            self.dataset.start(sess)

            # Load teacher model
            if self.FLAGS.load_file_tea:
                if self.FLAGS.alpha != 0:
                    self.model_tea.load_checkpoint(self.FLAGS.load_file_tea, self.sess, self.FLAGS.load_namescope_tea)
            if not self.FLAGS.load_file_stu:
                load_namescope_stu = self.FLAGS["teacher"]["namescope"] if self.FLAGS.load_namescope_tea is None else self.FLAGS.load_namescope_tea
                load_file_stu = self.FLAGS.load_file_tea
            else:
                load_namescope_stu = self.FLAGS.load_namescope_stu
                load_file_stu = self.FLAGS.load_file_stu
            # Load student model
            if self.FLAGS.use_denoiser:
                self.model_stu.load_checkpoint([self.FLAGS.load_file_den, load_file_stu], self.sess, [self.FLAGS.load_namescope_den, load_namescope_stu], exclude_pattern=self.FLAGS.load_exclude)
            else:
                self.model_stu.load_checkpoint(load_file_stu, self.sess, load_namescope_stu, exclude_pattern=self.FLAGS.load_exclude)
            for m, l_namescope, l_file in zip(self.additional_models, [m_cfg["load_namescope"] for m_cfg in self.FLAGS.additional_models], [m_cfg["checkpoint"] for m_cfg in self.FLAGS.additional_models]):
                m.load_checkpoint(l_file, self.sess, l_namescope)
            # Testing
            self.test(adv=True, name="test stu")
            if self.FLAGS.load_file_test: # additional student test models
                for i, test_model in enumerate(self.FLAGS.load_file_test):
                    self.model_stu.load_checkpoint(test_model, self.sess, load_namescope_stu, exclude_pattern=self.FLAGS.load_exclude)
                    self.test(adv=True, name="test additionan {} {}".format(i, os.path.basename(test_model)))
            elif self.FLAGS.test_saltpepper is not None:
                if isinstance(self.FLAGS.test_saltpepper, (tuple, list)):
                    for sp in self.FLAGS.test_saltpepper:
                        self.test(saltpepper=sp, adv=False, name="saltpepper_{}".format(sp))
                else:
                    self.test(saltpepper=self.FLAGS.test_saltpepper, adv=False, name="saltpepper_{}".format(self.FLAGS.test_saltpepper))
            self.dataset.end()
            sys.exit(0)

        # if not self.FLAGS.test_only
        if not self.FLAGS.scratch and ((not self.FLAGS.alpha or self.FLAGS.distill_self) and not self.FLAGS.load_file_stu) or ((self.FLAGS.alpha and not self.FLAGS.distill_self) and not self.FLAGS.load_file_tea):
            utils.log("error: no input file. Must supply teacher model for training with disstillation; or student model for training without distillation or distill self.")
            sys.exit(1)
        # Load teacher model
        if self.FLAGS.alpha != 0 and not self.FLAGS.distill_self:
            self.model_tea.load_checkpoint(self.FLAGS.load_file_tea, self.sess, self.FLAGS.load_namescope_tea)
        if not self.FLAGS.scratch: # if not train from scratch, load student model
            if not self.FLAGS.load_file_stu:
                load_namescope_stu = self.FLAGS["teacher"]["namescope"] if self.FLAGS.load_namescope_tea is None else self.FLAGS.load_namescope_tea
                load_file_stu = self.FLAGS.load_file_tea
            else:
                load_namescope_stu = self.FLAGS.load_namescope_stu
                load_file_stu = self.FLAGS.load_file_stu
            # Load student model
            if self.FLAGS.use_denoiser:
                self.model_stu.load_checkpoint([self.FLAGS.load_file_den, load_file_stu], self.sess, [self.FLAGS.load_namescope_den, load_namescope_stu], exclude_pattern=self.FLAGS.load_exclude)
            else:
                self.model_stu.load_checkpoint(load_file_stu, self.sess, load_namescope_stu, exclude_pattern=self.FLAGS.load_exclude)

        # Start the dataset threads; start the dataset after model stu is loaded, in case there are following models to be copied (for graybox dataset);
        # **NOTE**: this might incur a even longer delay for the init-test or the first training batch (these delay is not avoidable for graybox dataset)
        #           as copy ops are constructed here and
        #           the dataset's initialization is delayed compared to the previous implementation
        utils.log("Starting dataset...")
        self.dataset.start(sess)

        # Testing
        if not self.FLAGS.no_init_test:
            # assign this threshold value to model_stu.relu_thresh variable for initial test
            if self.FLAGS.relu_thresh_schedule is not None:
                sess.run(tf.assign(self.model_stu.relu_thresh, self.FLAGS.relu_thresh_schedule["start_lr"]))
            self.test(adv=True, name="loaded_teacher_copy")

        # Training
        self.num_addi_info = len(self.FLAGS.additional_info_attrs)
        self.info_attr_names = ["accuracy", "tea_accuracy", "loss"] + self.FLAGS.additional_info_attrs
        self.info_attrs = [getattr(self, name) for name in self.info_attr_names]
        print("will print additional informations during training: ", self.FLAGS.additional_info_attrs)
        utils.log("Start training...")
        self.train()

        self.dataset.end()

    @classmethod
    def populate_arguments(cls, parser):
        parser.add_argument("--load-file-stu", type=str, default="",
                            help="Load student model")
        parser.add_argument("--load-file-tea", type=str, default="",
                            help="Load teacher model")
        parser.add_argument("--load-namescope-stu", type=str, default=None,
                            help="The namescope of the student model")
        parser.add_argument("--load-namescope-tea", type=str, default=None,
                            help="The namescope of the teacher model")

        parser.add_argument("--load-exclude", metavar="PATTERN", action="append", default=[],
                            help="Exclude variables container PATTERN while loading from checkpoint")

        parser.add_argument("--use-denoiser", action="store_true", default=False)
        parser.add_argument("--load-file-den", type=str, default=None,
                            help="Load denoiser model")
        parser.add_argument("--load-namescope-den", type=str, default=None,
                            help="The namescope of the denoiser model")

        parser.add_argument("--scratch", action="store_true", help="training a model from scratch")
