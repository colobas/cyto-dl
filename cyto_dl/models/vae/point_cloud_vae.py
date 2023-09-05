import logging
from typing import Optional, Sequence, Union

import numpy as np
import torch
from omegaconf import DictConfig
from torch import nn

from cyto_dl.models.vae.base_vae import BaseVAE
from cyto_dl.models.vae.priors import IdentityPrior, IsotropicGaussianPrior
from cyto_dl.nn.losses import ChamferLoss, L1Loss
from cyto_dl.nn.point_cloud import DGCNN, FoldingNet, LocalDecoder

Array = Union[torch.Tensor, np.ndarray, Sequence[float]]
logger = logging.getLogger("lightning")
logger.propagate = False


class PointCloudVAE(BaseVAE):
    def __init__(
        self,
        latent_dim: int,
        x_label: str,
        num_points: int,
        hidden_dim=64,
        hidden_conv2d_channels: list = [64, 64, 64, 64],
        hidden_conv1d_channels: list = [512, 20],
        hidden_decoder_dim: int = 512,
        k=20,
        mode="scalar",
        get_rotation=False,
        include_cross=True,
        include_coords=True,
        id_label: Optional[str] = None,
        optimizer: torch.optim.Optimizer = torch.optim.Adam,
        beta: float = 1.0,
        embedding_prior: str = "identity",
        decoder_type: str = "foldingnet",
        loss_type: str = "chamfer",
        eps: float = 1e-6,
        shape: str = "sphere",
        num_coords: int = 3,
        std: float = 0.3,
        sphere_path: str = "/allen/aics/modeling/ritvik/projects/cellshape/cellshape-cloud/cellshape_cloud/vendor/sphere.npy",
        gaussian_path: str = "/allen/aics/modeling/ritvik/projects/cellshape/cellshape-cloud/cellshape_cloud/vendor/gaussian.npy",
        symmetry_breaking_axis: Optional[Union[str, int]] = None,
        scalar_inds: Optional[int] = None,
        generate_grid_feats: Optional[bool] = False,
        padding: Optional[float] = 0.1,
        reso_plane: Optional[int] = 64,
        plane_type: Optional[list] = ["xz", "xy", "yz"],
        scatter_type: Optional[str] = "max",
        point_label: Optional[str] = "points",
        occupancy_label: Optional[str] = "points.df",
        encoder: Optional[dict] = None,
        decoder: Optional[dict] = None,
        condition_encoder: Optional[dict] = None,
        condition_decoder: Optional[dict] = None,
        condition_keys: Optional[list] = None,
        reconstruction_loss: Optional[dict] = None,
        prior: Optional[dict] = None,
        **base_kwargs,
    ):
        self.get_rotation = get_rotation
        self.symmetry_breaking_axis = symmetry_breaking_axis
        self.scalar_inds = scalar_inds
        self.decoder_type = decoder_type
        self.generate_grid_feats = generate_grid_feats
        self.occupancy_label = occupancy_label
        self.point_label = point_label
        self.condition_keys = condition_keys

        if embedding_prior == "gaussian":
            self.encoder_out_size = 2 * latent_dim
        else:
            self.encoder_out_size = latent_dim

        if encoder is None:
            encoder = DGCNN(
                num_features=self.encoder_out_size,
                hidden_dim=hidden_dim,
                hidden_conv2d_channels=hidden_conv2d_channels,
                hidden_conv1d_channels=hidden_conv1d_channels,
                k=k,
                mode=mode,
                scalar_inds=scalar_inds,
                include_cross=include_cross,
                include_coords=include_coords,
                symmetry_breaking_axis=symmetry_breaking_axis,
                generate_grid_feats=generate_grid_feats,
                padding=padding,
                reso_plane=reso_plane,
                plane_type=plane_type,
                scatter_type=scatter_type,
            )
            encoder = {x_label: encoder}

        if decoder is None:
            if decoder_type == "foldingnet":
                decoder = FoldingNet(
                    latent_dim,
                    num_points,
                    hidden_decoder_dim,
                    std,
                    shape,
                    sphere_path,
                    gaussian_path,
                    num_coords,
                )
            elif decoder_type == "localdecoder":
                decoder = LocalDecoder(latent_dim, hidden_decoder_dim)
            decoder = {x_label: decoder}

        if reconstruction_loss is None:
            if loss_type == "chamfer":
                reconstruction_loss = {x_label: ChamferLoss()}
            elif loss_type == "L1":
                reconstruction_loss = {x_label: L1Loss()}

        if prior is None:
            prior = {
                "embedding": (
                    IsotropicGaussianPrior(dimensionality=latent_dim)
                    if embedding_prior == "gaussian"
                    else IdentityPrior(dimensionality=latent_dim)
                ),
            }

        if self.get_rotation:
            prior["rotation"] = IdentityPrior(dimensionality=1)

        super().__init__(
            encoder=encoder,
            decoder=decoder,
            latent_dim=latent_dim,
            x_label=x_label,
            id_label=id_label,
            beta=beta,
            reconstruction_loss=reconstruction_loss,
            optimizer=optimizer,
            prior=prior,
        )

        self.condition_encoder = nn.ModuleDict(condition_encoder)
        self.condition_decoder = nn.ModuleDict(condition_decoder)

    def decode(self, z_parts, return_canonical=False, batch=None):
        if hasattr(self.encoder[self.hparams.x_label], "generate_grid_feats"):
            if self.encoder[self.hparams.x_label].generate_grid_feats:
                base_xhat = self.decoder[self.hparams.x_label](
                    batch[self.point_label], z_parts["grid_feats"]
                )
            else:
                base_xhat = self.decoder[self.hparams.x_label](z_parts[self.hparams.x_label])
        else:
            base_xhat = self.decoder[self.hparams.x_label](z_parts[self.hparams.x_label])

        if self.get_rotation:
            rotation = z_parts["rotation"]
            xhat = torch.einsum("bij,bjk->bik", base_xhat[:, :, :3], rotation)
            if xhat.shape[-1] != base_xhat.shape[-1]:
                xhat = torch.cat([xhat, base_xhat[:, :, -1:]], dim=-1)
        else:
            xhat = base_xhat

        if return_canonical:
            return {self.hparams.x_label: xhat, "canonical": base_xhat}

        return {self.hparams.x_label: xhat}

    def encoder_compose_function(self, z_parts):
        if self.condition_keys:
            for j, key in enumerate([self.hparams.x_label] + self.condition_keys):
                this_z_parts = z_parts[key]
                if len(this_z_parts.shape) == 3:
                    this_z_parts = this_z_parts.argmax(dim=1)
                if j == 0:
                    cond_feats = this_z_parts
                else:
                    cond_feats = torch.cat((cond_feats, this_z_parts), dim=1)
            z_parts[self.hparams.x_label] = self.condition_encoder[self.hparams.x_label](
                cond_feats
            )
        return z_parts

    def decoder_compose_function(self, z_parts, batch):
        if self.condition_keys:
            for j, key in enumerate(self.condition_keys):
                if j == 0:
                    cond_inputs = batch[key]
                else:
                    cond_inputs = torch.cat((cond_inputs, batch[key]), dim=1)
                cond_feats = torch.cat((cond_inputs, z_parts[self.hparams.x_label]), dim=1)
            z_parts[self.hparams.x_label] = self.condition_decoder[self.hparams.x_label](
                cond_feats
            )
        return z_parts

    def calculate_rcl_dict(self, x, xhat):
        rcl_per_input_dimension = {}
        rcl_reduced = {}
        for key in xhat.keys():
            rcl_per_input_dimension[key] = self.calculate_rcl(x, xhat, key, self.occupancy_label)
            if len(rcl_per_input_dimension[key].shape) > 0:
                rcl = (
                    rcl_per_input_dimension[key]
                    # flatten
                    .view(rcl_per_input_dimension[key].shape[0], -1)
                    # and sum across each batch element's dimensions
                    .sum(dim=1)
                )

                rcl_reduced[key] = rcl.mean()
            else:
                rcl_reduced[key] = rcl_per_input_dimension[key]
        return rcl_reduced

    def forward(self, batch, decode=False, inference=True, return_params=False):
        is_inference = inference or not self.training

        z_params = self.encode(batch, get_rotation=self.get_rotation)
        z_params = self.encoder_compose_function(z_params)
        z = self.sample_z(z_params, inference=inference)

        z = self.decoder_compose_function(z, batch)

        if not decode:
            return z

        if hasattr(self.encoder[self.hparams.x_label], "generate_grid_feats"):
            if self.encoder[self.hparams.x_label].generate_grid_feats:
                xhat = self.decode(z, batch=batch)
            else:
                xhat = self.decode(z)
        else:
            xhat = self.decode(z)

        if return_params:
            return xhat, z, z_params

        return xhat, z
