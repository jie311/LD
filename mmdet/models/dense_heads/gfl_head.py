import torch
import torch.nn as nn
import torch.nn.functional as F

from mmcv.cnn import ConvModule, Scale, bias_init_with_prob, normal_init
from mmcv.runner import force_fp32
import numpy as np
from mmdet.core import (anchor_inside_flags, bbox2distance, bbox_overlaps,
                        build_assigner, build_sampler, distance2bbox,
                        images_to_levels, multi_apply, multiclass_nms,
                        reduce_mean, unmap)
from ..builder import HEADS, build_loss
from .anchor_head import AnchorHead


class Integral(nn.Module):
    """A fixed layer for calculating integral result from distribution.

    This layer calculates the target location by :math: `sum{P(y_i) * y_i}`,
    P(y_i) denotes the softmax vector that represents the discrete distribution
    y_i denotes the discrete set, usually {0, 1, 2, ..., reg_max}

    Args:
        reg_max (int): The maximal value of the discrete set. Default: 16. You
            may want to reset it according to your new dataset or related
            settings.
    """

    def __init__(self, reg_max=16):
        super(Integral, self).__init__()
        self.reg_max = reg_max
        self.register_buffer('project',
                             torch.linspace(0, self.reg_max, self.reg_max + 1))

    def forward(self, x):
        """Forward feature from the regression head to get integral result of
        bounding box location.

        Args:
            x (Tensor): Features of the regression head, shape (N, 4*(n+1)),
                n is self.reg_max.

        Returns:
            x (Tensor): Integral result of box locations, i.e., distance
                offsets from the box center in four directions, shape (N, 4).
        """
        x = F.softmax(x.reshape(-1, self.reg_max + 1), dim=1)
        x = F.linear(x, self.project.type_as(x)).reshape(-1, 4)
        return x


@HEADS.register_module()
class GFLHead(AnchorHead):
    """Generalized Focal Loss: Learning Qualified and Distributed Bounding
    Boxes for Dense Object Detection.

    GFL head structure is similar with ATSS, however GFL uses
    1) joint representation for classification and localization quality, and
    2) flexible General distribution for bounding box locations,
    which are supervised by
    Quality Focal Loss (QFL) and Distribution Focal Loss (DFL), respectively

    https://arxiv.org/abs/2006.04388

    Args:
        num_classes (int): Number of categories excluding the background
            category.
        in_channels (int): Number of channels in the input feature map.
        stacked_convs (int): Number of conv layers in cls and reg tower.
            Default: 4.
        conv_cfg (dict): dictionary to construct and config conv layer.
            Default: None.
        norm_cfg (dict): dictionary to construct and config norm layer.
            Default: dict(type='GN', num_groups=32, requires_grad=True).
        loss_qfl (dict): Config of Quality Focal Loss (QFL).
        reg_max (int): Max value of integral set :math: `{0, ..., reg_max}`
            in QFL setting. Default: 16.
    Example:
        >>> self = GFLHead(11, 7)
        >>> feats = [torch.rand(1, 7, s, s) for s in [4, 8, 16, 32, 64]]
        >>> cls_quality_score, bbox_pred = self.forward(feats)
        >>> assert len(cls_quality_score) == len(self.scales)
    """

    def __init__(self,
                 num_classes,
                 in_channels,
                 stacked_convs=4,
                 conv_cfg=None,
                 norm_cfg=dict(type='GN', num_groups=32, requires_grad=True),
                 loss_dfl=dict(type='DistributionFocalLoss', loss_weight=0.25),
                 reg_max=16,
                 **kwargs):
        self.stacked_convs = stacked_convs
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg
        self.reg_max = reg_max
        super(GFLHead, self).__init__(num_classes, in_channels, **kwargs)

        self.sampling = False
        if self.train_cfg:
            self.assigner = build_assigner(self.train_cfg.assigner)
            # SSD sampling=False so use PseudoSampler
            sampler_cfg = dict(type='PseudoSampler')
            self.sampler = build_sampler(sampler_cfg, context=self)

        self.integral = Integral(self.reg_max)
        self.loss_dfl = build_loss(loss_dfl)

    def _init_layers(self):
        """Initialize layers of the head."""
        self.relu = nn.ReLU(inplace=True)
        self.cls_convs = nn.ModuleList()
        self.reg_convs = nn.ModuleList()
        for i in range(self.stacked_convs):
            chn = self.in_channels if i == 0 else self.feat_channels
            self.cls_convs.append(
                ConvModule(
                    chn,
                    self.feat_channels,
                    3,
                    stride=1,
                    padding=1,
                    conv_cfg=self.conv_cfg,
                    norm_cfg=self.norm_cfg))
            self.reg_convs.append(
                ConvModule(
                    chn,
                    self.feat_channels,
                    3,
                    stride=1,
                    padding=1,
                    conv_cfg=self.conv_cfg,
                    norm_cfg=self.norm_cfg))
        assert self.num_anchors == 1, 'anchor free version'
        self.gfl_cls = nn.Conv2d(
            self.feat_channels, self.cls_out_channels, 3, padding=1)
        self.gfl_reg = nn.Conv2d(
            self.feat_channels, 4 * (self.reg_max + 1), 3, padding=1)
        self.scales = nn.ModuleList(
            [Scale(1.0) for _ in self.anchor_generator.strides])

    def init_weights(self):
        """Initialize weights of the head."""
        for m in self.cls_convs:
            normal_init(m.conv, std=0.01)
        for m in self.reg_convs:
            normal_init(m.conv, std=0.01)
        bias_cls = bias_init_with_prob(0.01)
        normal_init(self.gfl_cls, std=0.01, bias=bias_cls)
        normal_init(self.gfl_reg, std=0.01)

    def forward(self, feats):
        """Forward features from the upstream network.

        Args:
            feats (tuple[Tensor]): Features from the upstream network, each is
                a 4D-tensor.

        Returns:
            tuple: Usually a tuple of classification scores and bbox prediction
                cls_scores (list[Tensor]): Classification and quality (IoU)
                    joint scores for all scale levels, each is a 4D-tensor,
                    the channel number is num_classes.
                bbox_preds (list[Tensor]): Box distribution logits for all
                    scale levels, each is a 4D-tensor, the channel number is
                    4*(n+1), n is max value of integral set.
        """
        return multi_apply(self.forward_single, feats, self.scales)

    def forward_single(self, x, scale):
        """Forward feature of a single scale level.

        Args:
            x (Tensor): Features of a single scale level.
            scale (:obj: `mmcv.cnn.Scale`): Learnable scale module to resize
                the bbox prediction.

        Returns:
            tuple:
                cls_score (Tensor): Cls and quality joint scores for a single
                    scale level the channel number is num_classes.
                bbox_pred (Tensor): Box distribution logits for a single scale
                    level, the channel number is 4*(n+1), n is max value of
                    integral set.
        """
        cls_feat = x
        reg_feat = x
        for cls_conv in self.cls_convs:
            cls_feat = cls_conv(cls_feat)
        for reg_conv in self.reg_convs:
            reg_feat = reg_conv(reg_feat)
        cls_score = self.gfl_cls(cls_feat)
        bbox_pred = scale(self.gfl_reg(reg_feat)).float()
        return cls_score, bbox_pred

    def anchor_center(self, anchors):
        """Get anchor centers from anchors.

        Args:
            anchors (Tensor): Anchor list with shape (N, 4), "xyxy" format.

        Returns:
            Tensor: Anchor centers with shape (N, 2), "xy" format.
        """
        anchors_cx = (anchors[..., 2] + anchors[..., 0]) / 2
        anchors_cy = (anchors[..., 3] + anchors[..., 1]) / 2
        return torch.stack([anchors_cx, anchors_cy], dim=-1)

    def loss_single(self, anchors, cls_score, bbox_pred, labels, label_weights,
                    bbox_targets, labels_neg, label_weights_neg,
                    bbox_targets_neg, stride, assigned_neg, num_total_samples,
                    num_total_samples_neg):
        """Compute loss of a single scale level.

        Args:
            anchors (Tensor): Box reference for each scale level with shape
                (N, num_total_anchors, 4).
            cls_score (Tensor): Cls and quality joint scores for each scale
                level has shape (N, num_classes, H, W).
            bbox_pred (Tensor): Box distribution logits for each scale
                level with shape (N, 4*(n+1), H, W), n is max value of integral
                set.
            labels (Tensor): Labels of each anchors with shape
                (N, num_total_anchors).
            label_weights (Tensor): Label weights of each anchor with shape
                (N, num_total_anchors)
            bbox_targets (Tensor): BBox regression targets of each anchor wight
                shape (N, num_total_anchors, 4).
            stride (tuple): Stride in this scale level.
            num_total_samples (int): Number of positive samples that is
                reduced over all GPUs.

        Returns:
            dict[str, Tensor]: A dictionary of loss components.
        """
        assert stride[0] == stride[1], 'h stride is not equal to w stride!'
        anchors = anchors.reshape(-1, 4)
        cls_score = cls_score.permute(0, 2, 3,
                                      1).reshape(-1, self.cls_out_channels)
        bbox_pred = bbox_pred.permute(0, 2, 3,
                                      1).reshape(-1, 4 * (self.reg_max + 1))
        bbox_targets = bbox_targets.reshape(-1, 4)
        labels = labels.reshape(-1)
        label_weights = label_weights.reshape(-1)

        bbox_targets_neg = bbox_targets_neg.reshape(-1, 4)
        labels_neg = labels_neg.reshape(-1)
        label_weights_neg = label_weights_neg.reshape(-1)
        assigned_neg = assigned_neg.reshape(-1)

        # FG cat_id: [0, num_classes -1], BG cat_id: num_classes
        bg_class_ind = self.num_classes
        pos_inds = ((labels >= 0)
                    & (labels < bg_class_ind)).nonzero().squeeze(1)
        pos_inds_neg = ((labels_neg >= 0)
                        & (labels_neg < bg_class_ind)).nonzero().squeeze(1)

        score = label_weights.new_zeros(labels.shape)
        score_neg = label_weights_neg.new_zeros(labels_neg.shape)
        remain_inds = (assigned_neg > 0).nonzero().squeeze(1)
        if len(pos_inds) > 0:
            pos_bbox_targets = bbox_targets[pos_inds]
            pos_bbox_pred = bbox_pred[pos_inds]
            pos_anchors = anchors[pos_inds]
            pos_anchor_centers = self.anchor_center(pos_anchors) / stride[0]

            weight_targets = cls_score.detach().sigmoid()
            weight_targets = weight_targets.max(dim=1)[0][pos_inds]
            pos_bbox_pred_corners = self.integral(pos_bbox_pred)
            pos_decode_bbox_pred = distance2bbox(pos_anchor_centers,
                                                 pos_bbox_pred_corners)
            pos_decode_bbox_targets = pos_bbox_targets / stride[0]
            score[pos_inds] = bbox_overlaps(
                pos_decode_bbox_pred.detach(),
                pos_decode_bbox_targets,
                is_aligned=True)
            pred_corners = pos_bbox_pred.reshape(-1, self.reg_max + 1)
            target_corners = bbox2distance(pos_anchor_centers,
                                           pos_decode_bbox_targets,
                                           self.reg_max).reshape(-1)

            # regression loss
            loss_bbox = self.loss_bbox(
                pos_decode_bbox_pred,
                pos_decode_bbox_targets,
                weight=weight_targets,
                avg_factor=1.0)

            # dfl loss
            loss_dfl = self.loss_dfl(
                pred_corners,
                target_corners,
                weight=weight_targets[:, None].expand(-1, 4).reshape(-1),
                avg_factor=4.0)
        else:
            loss_bbox = bbox_pred.sum() * 0
            loss_dfl = bbox_pred.sum() * 0
            weight_targets = bbox_pred.new_tensor(0)

        if len(pos_inds_neg) > 0:
            pos_bbox_targets_neg = bbox_targets_neg[pos_inds_neg]
            pos_bbox_pred_neg = bbox_pred[pos_inds_neg]
            pos_anchors_neg = anchors[pos_inds_neg]
            pos_anchor_centers_neg = self.anchor_center(
                pos_anchors_neg) / stride[0]

            weight_targetss = ((cls_score.detach().sigmoid().max(dim=1)[0]) <
                               0).float()
            weight_targets_neg = weight_targetss[remain_inds] + assigned_neg[
                remain_inds]

            pos_bbox_pred_corners_neg = self.integral(pos_bbox_pred_neg)
            pos_decode_bbox_pred_neg = distance2bbox(
                pos_anchor_centers_neg, pos_bbox_pred_corners_neg)
            pos_decode_bbox_targets_neg = pos_bbox_targets_neg / stride[0]
            '''
            score[pos_inds_neg] = bbox_overlaps(
                pos_decode_bbox_pred_neg.detach(),
                pos_decode_bbox_targets_neg,
                is_aligned=True)**6
            '''
            pred_corners_neg = pos_bbox_pred_neg.reshape(-1, self.reg_max + 1)
            target_corners_neg = bbox2distance(pos_anchor_centers_neg,
                                               pos_decode_bbox_targets_neg,
                                               self.reg_max).reshape(-1)

            # regression loss
            loss_bbox_neg = 0.125 * self.loss_bbox(
                pos_decode_bbox_pred_neg,
                pos_decode_bbox_targets_neg,
                weight=weight_targets_neg,
                avg_factor=1.0)

            # dfl loss
            loss_dfl_neg = 0.125 * self.loss_dfl(
                pred_corners_neg,
                target_corners_neg,
                weight=weight_targets_neg[:, None].expand(-1, 4).reshape(-1),
                avg_factor=4.0)
            '''
            loss_cls = self.loss_cls(
                cls_score, (labels, score), pos_inds_neg,
                weight=label_weights,
                avg_factor=num_total_samples)

            if self.use_sigmoid:
                loss_cls = self.loss_weight * quality_focal_loss1(cls_score[pos_inds_neg],(labels[pos_inds_neg], score[pos_inds_neg]),label_weights,beta=self.beta,reduction=reduction,avg_factor=avg_factor)
            else:
                raise NotImplementedError
            '''
        else:
            loss_bbox_neg = bbox_pred.sum() * 0
            loss_dfl_neg = bbox_pred.sum() * 0
            weight_targets_neg = bbox_pred.new_tensor(0)

        # cls (qfl) loss
        loss_cls = self.loss_cls(
            cls_score, (labels, labels_neg, pos_inds_neg, score),
            weight=label_weights,
            avg_factor=num_total_samples)

        return loss_cls, loss_bbox, loss_dfl, weight_targets.sum(
        ), loss_bbox_neg, loss_dfl_neg, weight_targets_neg.sum()

    @force_fp32(apply_to=('cls_scores', 'bbox_preds'))
    def loss(self,
             cls_scores,
             bbox_preds,
             gt_bboxes,
             gt_labels,
             img_metas,
             gt_bboxes_ignore=None):
        """Compute losses of the head.

        Args:
            cls_scores (list[Tensor]): Cls and quality scores for each scale
                level has shape (N, num_classes, H, W).
            bbox_preds (list[Tensor]): Box distribution logits for each scale
                level with shape (N, 4*(n+1), H, W), n is max value of integral
                set.
            gt_bboxes (list[Tensor]): Ground truth bboxes for each image with
                shape (num_gts, 4) in [tl_x, tl_y, br_x, br_y] format.
            gt_labels (list[Tensor]): class indices corresponding to each box
            img_metas (list[dict]): Meta information of each image, e.g.,
                image size, scaling factor, etc.
            gt_bboxes_ignore (list[Tensor] | None): specify which bounding
                boxes can be ignored when computing the loss.

        Returns:
            dict[str, Tensor]: A dictionary of loss components.
        """

        featmap_sizes = [featmap.size()[-2:] for featmap in cls_scores]
        assert len(featmap_sizes) == self.anchor_generator.num_levels

        device = cls_scores[0].device
        anchor_list, valid_flag_list = self.get_anchors(
            featmap_sizes, img_metas, device=device)
        label_channels = self.cls_out_channels if self.use_sigmoid_cls else 1

        cls_reg_targets = self.get_targets(
            anchor_list,
            valid_flag_list,
            gt_bboxes,
            img_metas,
            gt_bboxes_ignore_list=gt_bboxes_ignore,
            gt_labels_list=gt_labels,
            label_channels=label_channels)
        if cls_reg_targets is None:
            return None

        #(anchor_list, labels_list, label_weights_list, bbox_targets_list,
        #bbox_weights_list, num_total_pos, num_total_neg) = cls_reg_targets

        (anchor_list, labels_list, label_weights_list, bbox_targets_list,
         bbox_weights_list, num_total_pos, num_total_neg, labels_list_neg,
         label_weights_list_neg, bbox_targets_list_neg, bbox_weights_list_neg,
         num_total_pos_neg, num_total_neg_neg,
         assigned_neg_list) = cls_reg_targets

        num_total_samples = reduce_mean(
            torch.tensor(num_total_pos, dtype=torch.float,
                         device=device)).item()
        num_total_samples = max(num_total_samples, 1.0)

        num_total_samples_neg = reduce_mean(
            torch.tensor(num_total_pos_neg, dtype=torch.float,
                         device=device)).item()
        num_total_samples_neg = max(num_total_samples_neg, 1.0)

        losses_cls, losses_bbox, losses_dfl, avg_factor, losses_bbox_neg, losses_dfl_neg,\
            avg_factor_neg = multi_apply(
                self.loss_single,
                anchor_list,
                cls_scores,
                bbox_preds,
                labels_list,
                label_weights_list,
                bbox_targets_list,
                labels_list_neg,
                label_weights_list_neg,
                bbox_targets_list_neg,
                self.anchor_generator.strides,
                assigned_neg_list,
                num_total_samples=num_total_samples,
                num_total_samples_neg=num_total_samples_neg)
        avg_factor = sum(avg_factor)
        avg_factor = reduce_mean(avg_factor).item()
        losses_bbox = list(map(lambda x: x / avg_factor, losses_bbox))
        losses_dfl = list(map(lambda x: x / avg_factor, losses_dfl))

        avg_factor_neg = sum(avg_factor_neg)
        avg_factor_neg = reduce_mean(avg_factor_neg).item()

        losses_bbox_neg = list(
            map(lambda x: x / avg_factor_neg, losses_bbox_neg))
        losses_dfl_neg = list(
            map(lambda x: x / avg_factor_neg, losses_dfl_neg))

        return dict(
            loss_cls=losses_cls,
            loss_bbox=losses_bbox,
            loss_dfl=losses_dfl,
            loss_bbox_neg=losses_bbox_neg,
            loss_dfl_neg=losses_dfl_neg)

    def _get_bboxes(self,
                    cls_scores,
                    bbox_preds,
                    mlvl_anchors,
                    img_shapes,
                    scale_factors,
                    cfg,
                    rescale=False,
                    with_nms=True):
        """Transform outputs for a single batch item into labeled boxes.

        Args:
            cls_scores (list[Tensor]): Box scores for a single scale level
                has shape (N, num_classes, H, W).
            bbox_preds (list[Tensor]): Box distribution logits for a single
                scale level with shape (N, 4*(n+1), H, W), n is max value of
                integral set.
            mlvl_anchors (list[Tensor]): Box reference for a single scale level
                with shape (num_total_anchors, 4).
            img_shapes (list[tuple[int]]): Shape of the input image,
                list[(height, width, 3)].
            scale_factors (list[ndarray]): Scale factor of the image arange as
                (w_scale, h_scale, w_scale, h_scale).
            cfg (mmcv.Config | None): Test / postprocessing configuration,
                if None, test_cfg would be used.
            rescale (bool): If True, return boxes in original image space.
                Default: False.
            with_nms (bool): If True, do nms before return boxes.
                Default: True.

        Returns:
            list[tuple[Tensor, Tensor]]: Each item in result_list is 2-tuple.
                The first item is an (n, 5) tensor, where 5 represent
                (tl_x, tl_y, br_x, br_y, score) and the score between 0 and 1.
                The shape of the second tensor in the tuple is (n,), and
                each element represents the class label of the corresponding
                box.
        """
        cfg = self.test_cfg if cfg is None else cfg
        assert len(cls_scores) == len(bbox_preds) == len(mlvl_anchors)
        batch_size = cls_scores[0].shape[0]

        mlvl_bboxes = []
        mlvl_scores = []
        for cls_score, bbox_pred, stride, anchors in zip(
                cls_scores, bbox_preds, self.anchor_generator.strides,
                mlvl_anchors):
            assert cls_score.size()[-2:] == bbox_pred.size()[-2:]
            assert stride[0] == stride[1]
            scores = cls_score.permute(0, 2, 3, 1).reshape(
                batch_size, -1, self.cls_out_channels).sigmoid()
            bbox_pred = bbox_pred.permute(0, 2, 3, 1)

            bbox_pred = self.integral(bbox_pred) * stride[0]
            bbox_pred = bbox_pred.reshape(batch_size, -1, 4)

            nms_pre = cfg.get('nms_pre', -1)
            if nms_pre > 0 and scores.shape[1] > nms_pre:
                max_scores, _ = scores.max(-1)
                _, topk_inds = max_scores.topk(nms_pre)
                batch_inds = torch.arange(batch_size).view(
                    -1, 1).expand_as(topk_inds).long()
                anchors = anchors[topk_inds, :]
                bbox_pred = bbox_pred[batch_inds, topk_inds, :]
                scores = scores[batch_inds, topk_inds, :]
            else:
                anchors = anchors.expand_as(bbox_pred)

            bboxes = distance2bbox(
                self.anchor_center(anchors), bbox_pred, max_shape=img_shapes)
            mlvl_bboxes.append(bboxes)
            mlvl_scores.append(scores)

        batch_mlvl_bboxes = torch.cat(mlvl_bboxes, dim=1)
        if rescale:
            batch_mlvl_bboxes /= batch_mlvl_bboxes.new_tensor(
                scale_factors).unsqueeze(1)

        batch_mlvl_scores = torch.cat(mlvl_scores, dim=1)
        # Add a dummy background class to the backend when using sigmoid
        # remind that we set FG labels to [0, num_class-1] since mmdet v2.0
        # BG cat_id: num_class
        padding = batch_mlvl_scores.new_zeros(batch_size,
                                              batch_mlvl_scores.shape[1], 1)
        batch_mlvl_scores = torch.cat([batch_mlvl_scores, padding], dim=-1)

        if with_nms:
            det_results = []
            for (mlvl_bboxes, mlvl_scores) in zip(batch_mlvl_bboxes,
                                                  batch_mlvl_scores):
                det_bbox, det_label = multiclass_nms(mlvl_bboxes, mlvl_scores,
                                                     cfg.score_thr, cfg.nms,
                                                     cfg.max_per_img)
                det_results.append(tuple([det_bbox, det_label]))
        else:
            det_results = [
                tuple(mlvl_bs)
                for mlvl_bs in zip(batch_mlvl_bboxes, batch_mlvl_scores)
            ]
        return det_results

    def get_targets(self,
                    anchor_list,
                    valid_flag_list,
                    gt_bboxes_list,
                    img_metas,
                    gt_bboxes_ignore_list=None,
                    gt_labels_list=None,
                    label_channels=1,
                    unmap_outputs=True):
        """Get targets for GFL head.

        This method is almost the same as `AnchorHead.get_targets()`. Besides
        returning the targets as the parent method does, it also returns the
        anchors as the first element of the returned tuple.
        """
        num_imgs = len(img_metas)
        assert len(anchor_list) == len(valid_flag_list) == num_imgs

        # anchor number of multi levels
        num_level_anchors = [anchors.size(0) for anchors in anchor_list[0]]
        num_level_anchors_list = [num_level_anchors] * num_imgs

        # concat all level anchors and flags to a single tensor
        for i in range(num_imgs):
            assert len(anchor_list[i]) == len(valid_flag_list[i])
            anchor_list[i] = torch.cat(anchor_list[i])
            valid_flag_list[i] = torch.cat(valid_flag_list[i])

        # compute targets for each image
        if gt_bboxes_ignore_list is None:
            gt_bboxes_ignore_list = [None for _ in range(num_imgs)]
        if gt_labels_list is None:
            gt_labels_list = [None for _ in range(num_imgs)]
        '''
        (all_anchors, all_labels, all_label_weights, all_bbox_targets,
         all_bbox_weights, pos_inds_list, neg_inds_list, all_assigned_neg, assigned_neg_inds_list) = multi_apply(
             self._get_target_single,
             anchor_list,
             valid_flag_list,
             num_level_anchors_list,
             gt_bboxes_list,
             gt_bboxes_ignore_list,
             gt_labels_list,
             img_metas,
             label_channels=label_channels,
             unmap_outputs=unmap_outputs)
        '''
        (all_anchors, all_labels, all_label_weights, all_bbox_targets,
         all_bbox_weights, pos_inds_list, neg_inds_list, all_labels_neg,
         all_label_weights_neg, all_bbox_targets_neg, all_bbox_weights_neg,
         pos_inds_list_neg, neg_inds_list_neg, all_assigned_neg) = multi_apply(
             self._get_target_single,
             anchor_list,
             valid_flag_list,
             num_level_anchors_list,
             gt_bboxes_list,
             gt_bboxes_ignore_list,
             gt_labels_list,
             img_metas,
             label_channels=label_channels,
             unmap_outputs=unmap_outputs)

        # no valid anchors
        if any([labels is None for labels in all_labels]):
            return None
        # sampled anchors of all images
        num_total_pos = sum([max(inds.numel(), 1) for inds in pos_inds_list])
        num_total_neg = sum([max(inds.numel(), 1) for inds in neg_inds_list])
        #num_total_remain_neg = sum([max(inds.numel(), 1) for inds in assigned_neg_inds_list])
        # split targets to a list w.r.t. multiple levels
        anchors_list = images_to_levels(all_anchors, num_level_anchors)
        labels_list = images_to_levels(all_labels, num_level_anchors)
        label_weights_list = images_to_levels(all_label_weights,
                                              num_level_anchors)
        bbox_targets_list = images_to_levels(all_bbox_targets,
                                             num_level_anchors)
        bbox_weights_list = images_to_levels(all_bbox_weights,
                                             num_level_anchors)
        assigned_neg_list = images_to_levels(all_assigned_neg,
                                             num_level_anchors)

        # sampled anchors of all images
        num_total_pos_neg = sum(
            [max(inds.numel(), 1) for inds in pos_inds_list_neg])
        num_total_neg_neg = sum(
            [max(inds.numel(), 1) for inds in neg_inds_list_neg])
        # split targets to a list w.r.t. multiple levels
        labels_list_neg = images_to_levels(all_labels_neg, num_level_anchors)
        label_weights_list_neg = images_to_levels(all_label_weights_neg,
                                                  num_level_anchors)
        bbox_targets_list_neg = images_to_levels(all_bbox_targets_neg,
                                                 num_level_anchors)
        bbox_weights_list_neg = images_to_levels(all_bbox_weights_neg,
                                                 num_level_anchors)
        #assigned_neg_list = images_to_levels(all_assigned_neg,
        #num_level_anchors)

        return (anchors_list, labels_list, label_weights_list,
                bbox_targets_list, bbox_weights_list, num_total_pos,
                num_total_neg, labels_list_neg, label_weights_list_neg,
                bbox_targets_list_neg, bbox_weights_list_neg,
                num_total_pos_neg, num_total_neg_neg, assigned_neg_list)

    # def _get_target_single(self,
    #                        flat_anchors,
    #                        valid_flags,
    #                        num_level_anchors,
    #                        gt_bboxes,
    #                        gt_bboxes_ignore,
    #                        gt_labels,
    #                        img_meta,
    #                        label_channels=1,
    #                        unmap_outputs=True):
    #     """Compute regression, classification targets for anchors in a single
    #     image.

    #     Args:
    #         flat_anchors (Tensor): Multi-level anchors of the image, which are
    #             concatenated into a single tensor of shape (num_anchors, 4)
    #         valid_flags (Tensor): Multi level valid flags of the image,
    #             which are concatenated into a single tensor of
    #                 shape (num_anchors,).
    #         num_level_anchors Tensor): Number of anchors of each scale level.
    #         gt_bboxes (Tensor): Ground truth bboxes of the image,
    #             shape (num_gts, 4).
    #         gt_bboxes_ignore (Tensor): Ground truth bboxes to be
    #             ignored, shape (num_ignored_gts, 4).
    #         gt_labels (Tensor): Ground truth labels of each box,
    #             shape (num_gts,).
    #         img_meta (dict): Meta info of the image.
    #         label_channels (int): Channel of label.
    #         unmap_outputs (bool): Whether to map outputs back to the original
    #             set of anchors.

    #     Returns:
    #         tuple: N is the number of total anchors in the image.
    #             anchors (Tensor): All anchors in the image with shape (N, 4).
    #             labels (Tensor): Labels of all anchors in the image with shape
    #                 (N,).
    #             label_weights (Tensor): Label weights of all anchor in the
    #                 image with shape (N,).
    #             bbox_targets (Tensor): BBox targets of all anchors in the
    #                 image with shape (N, 4).
    #             bbox_weights (Tensor): BBox weights of all anchors in the
    #                 image with shape (N, 4).
    #             pos_inds (Tensor): Indices of postive anchor with shape
    #                 (num_pos,).
    #             neg_inds (Tensor): Indices of negative anchor with shape
    #                 (num_neg,).
    #     """
    #     inside_flags = anchor_inside_flags(flat_anchors, valid_flags,
    #                                        img_meta['img_shape'][:2],
    #                                        self.train_cfg.allowed_border)
    #     if not inside_flags.any():
    #         return (None, ) * 7
    #     # assign gt and sample anchors
    #     anchors = flat_anchors[inside_flags, :]

    #     num_level_anchors_inside = self.get_num_level_anchors_inside(
    #         num_level_anchors, inside_flags)
    #     #assign_result, assigned_neg, assigned_neg_inds = self.assigner.assign(anchors, num_level_anchors_inside,
    #     #gt_bboxes, gt_bboxes_ignore,
    #     #gt_labels)
    #     assign_result = self.assigner.assign_pos(anchors,
    #                                              num_level_anchors_inside,
    #                                              gt_bboxes, gt_bboxes_ignore,
    #                                              gt_labels)

    #     sampling_result = self.sampler.sample(assign_result, anchors,
    #                                           gt_bboxes)

    #     assign_result_neg, assigned_neg = self.assigner.assign_neg(
    #         anchors, num_level_anchors_inside, gt_bboxes, gt_bboxes_ignore,
    #         gt_labels)

    #     sampling_result_neg = self.sampler.sample(assign_result_neg, anchors,
    #                                               gt_bboxes)

    #     num_valid_anchors = anchors.shape[0]
    #     bbox_targets = torch.zeros_like(anchors)
    #     bbox_weights = torch.zeros_like(anchors)
    #     bbox_targets_neg = torch.zeros_like(anchors)
    #     bbox_weights_neg = torch.zeros_like(anchors)

    #     labels = anchors.new_full((num_valid_anchors, ),
    #                               self.num_classes,
    #                               dtype=torch.long)
    #     labels_neg = anchors.new_full((num_valid_anchors, ),
    #                                   self.num_classes,
    #                                   dtype=torch.long)

    #     label_weights = anchors.new_zeros(num_valid_anchors, dtype=torch.float)
    #     label_weights_neg = anchors.new_zeros(
    #         num_valid_anchors, dtype=torch.float)
    #     pos_inds = sampling_result.pos_inds
    #     neg_inds = sampling_result.neg_inds
    #     pos_inds_neg = sampling_result_neg.pos_inds
    #     neg_inds_neg = sampling_result_neg.neg_inds

    #     if len(pos_inds) > 0:
    #         pos_bbox_targets = sampling_result.pos_gt_bboxes
    #         bbox_targets[pos_inds, :] = pos_bbox_targets
    #         bbox_weights[pos_inds, :] = 1.0

    #         if gt_labels is None:
    #             # Only rpn gives gt_labels as None
    #             # Foreground is the first class
    #             labels[pos_inds] = 0
    #         else:
    #             labels[pos_inds] = gt_labels[
    #                 sampling_result.pos_assigned_gt_inds]
    #         if self.train_cfg.pos_weight <= 0:
    #             label_weights[pos_inds] = 1.0
    #         else:
    #             label_weights[pos_inds] = self.train_cfg.pos_weight
    #     if len(neg_inds) > 0:
    #         label_weights[neg_inds] = 1.0

    #     if len(pos_inds_neg) > 0:
    #         pos_bbox_targets_neg = sampling_result_neg.pos_gt_bboxes
    #         bbox_targets_neg[pos_inds_neg, :] = pos_bbox_targets_neg
    #         bbox_weights_neg[pos_inds_neg, :] = 1.0

    #         if gt_labels is None:
    #             # Only rpn gives gt_labels as None
    #             # Foreground is the first class
    #             labels_neg[pos_inds_neg] = 0
    #         else:
    #             labels_neg[pos_inds_neg] = gt_labels[
    #                 sampling_result_neg.pos_assigned_gt_inds]
    #         if self.train_cfg.pos_weight <= 0:
    #             label_weights_neg[pos_inds_neg] = 1.0
    #         else:
    #             label_weights_neg[pos_inds_neg] = self.train_cfg.pos_weight
    #     if len(neg_inds_neg) > 0:
    #         label_weights_neg[neg_inds_neg] = 1.0

    #     # map up to original set of anchors
    #     if unmap_outputs:
    #         num_total_anchors = flat_anchors.size(0)
    #         anchors = unmap(anchors, num_total_anchors, inside_flags)
    #         labels = unmap(
    #             labels, num_total_anchors, inside_flags, fill=self.num_classes)
    #         label_weights = unmap(label_weights, num_total_anchors,
    #                               inside_flags)
    #         bbox_targets = unmap(bbox_targets, num_total_anchors, inside_flags)
    #         bbox_weights = unmap(bbox_weights, num_total_anchors, inside_flags)
    #         assigned_neg = unmap(assigned_neg, num_total_anchors, inside_flags)

    #         labels_neg = unmap(
    #             labels_neg,
    #             num_total_anchors,
    #             inside_flags,
    #             fill=self.num_classes)
    #         label_weights_neg = unmap(label_weights_neg, num_total_anchors,
    #                                   inside_flags)
    #         bbox_targets_neg = unmap(bbox_targets_neg, num_total_anchors,
    #                                  inside_flags)
    #         bbox_weights_neg = unmap(bbox_weights_neg, num_total_anchors,
    #                                  inside_flags)

    #     return (anchors, labels, label_weights, bbox_targets, bbox_weights,
    #             pos_inds, neg_inds, labels_neg, label_weights_neg,
    #             bbox_targets_neg, bbox_weights_neg, pos_inds_neg, neg_inds_neg,
    #             assigned_neg)

    def get_num_level_anchors_inside(self, num_level_anchors, inside_flags):
        split_inside_flags = torch.split(inside_flags, num_level_anchors)
        num_level_anchors_inside = [
            int(flags.sum()) for flags in split_inside_flags
        ]
        return num_level_anchors_inside
