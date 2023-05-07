from collections import OrderedDict
from functools import partial

import torch
import torch.nn.functional as F
from torch import Tensor

from torch import nn
import math
import warnings
import re

from typing import Dict, Iterable, List, Optional, Tuple, Union
from einops import rearrange, repeat


def MULTIMAE(lr_scale: float, image_size: int, is_colab: bool = True):
    """MULTIMAE

    Args:
        lr_scale (float): Scale learning rate of untrained parameters
        image_size (int): image size
        is_colab (bool, optional): Whether run this code in Colab or not. Defaults to True.
    """

    def pair(t):
        return t if isinstance(t, tuple) else (t, t)

    def build_2d_sincos_posemb(h, w, embed_dim=1024, temperature=10000.0):
        """Sine-cosine positional embeddings from MoCo-v3

        Source: https://github.com/facebookresearch/moco-v3/blob/main/vits.py
        """
        grid_w = torch.arange(w, dtype=torch.float32)
        grid_h = torch.arange(h, dtype=torch.float32)
        grid_w, grid_h = torch.meshgrid(grid_w, grid_h)
        assert (
            embed_dim % 4 == 0
        ), "Embed dimension must be divisible by 4 for 2D sin-cos position embedding"
        pos_dim = embed_dim // 4
        omega = torch.arange(pos_dim, dtype=torch.float32) / pos_dim
        omega = 1.0 / (temperature**omega)
        out_w = torch.einsum("m,d->md", [grid_w.flatten(), omega])
        out_h = torch.einsum("m,d->md", [grid_h.flatten(), omega])
        pos_emb = torch.cat(
            [torch.sin(out_w), torch.cos(out_w), torch.sin(out_h), torch.cos(out_h)],
            dim=1,
        )[None, :, :]
        pos_emb = rearrange(pos_emb, "b (h w) d -> b d h w", h=h, w=w, d=embed_dim)
        return pos_emb

    def _no_grad_trunc_normal_(tensor, mean, std, a, b):
        # Cut & paste from PyTorch official master until it's in a few official releases - RW
        # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
        def norm_cdf(x):
            # Computes standard normal cumulative distribution function
            return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

        if (mean < a - 2 * std) or (mean > b + 2 * std):
            warnings.warn(
                "mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                "The distribution of values may be incorrect.",
                stacklevel=2,
            )

        with torch.no_grad():
            # Values are generated by using a truncated uniform distribution and
            # then using the inverse CDF for the normal distribution.
            # Get upper and lower cdf values
            l = norm_cdf((a - mean) / std)
            u = norm_cdf((b - mean) / std)

            # Uniformly fill tensor with values from [l, u], then translate to
            # [2l-1, 2u-1].
            tensor.uniform_(2 * l - 1, 2 * u - 1)

            # Use inverse cdf transform for normal distribution to get truncated
            # standard normal
            tensor.erfinv_()

            # Transform to proper mean, std
            tensor.mul_(std * math.sqrt(2.0))
            tensor.add_(mean)

            # Clamp to ensure it's in the proper range
            tensor.clamp_(min=a, max=b)
            return tensor

    def trunc_normal_(tensor, mean=0.0, std=1.0, a=-2.0, b=2.0):
        # type: (Tensor, float, float, float, float) -> Tensor
        r"""Fills the input Tensor with values drawn from a truncated
        normal distribution. The values are effectively drawn from the
        normal distribution :math:`\mathcal{N}(\text{mean}, \text{std}^2)`
        with values outside :math:`[a, b]` redrawn until they are within
        the bounds. The method used for generating the random values works
        best when :math:`a \leq \text{mean} \leq b`.
        Args:
            tensor: an n-dimensional `torch.Tensor`
            mean: the mean of the normal distribution
            std: the standard deviation of the normal distribution
            a: the minimum cutoff value
            b: the maximum cutoff value
        Examples:
            >>> w = torch.empty(3, 5)
            >>> nn.init.trunc_normal_(w)
        """
        return _no_grad_trunc_normal_(tensor, mean, std, a, b)

    class PatchedInputAdapter(nn.Module):
        """Adapter for spatial inputs, like images or feature maps.
        Creates tokens from patches over the image.

        :param num_channels: Number of input channels of the image/feature map
        :param stride_level: Stride level compared to the full-sized image.
            E.g. 4 for 1/4th the size of the image.
        :param patch_size_full: Int or tuple of the patch size over the full image size.
            Patch size for smaller inputs will be computed accordingly.
        :param dim_tokens: Dimension of output tokens. Can be set using init method.
        :param sincos_pos_emb: Set to True (default) to use fixed 2D sin-cos positional embeddings
        :param learnable_pos_emb: Set to True to learn positional embeddings instead
        :param image_size: Default image size. Used to initialize size of positional embeddings.
        """

        def __init__(
            self,
            num_channels: int,
            stride_level: int,
            patch_size_full: Union[int, Tuple[int, int]],
            dim_tokens: Optional[int] = None,
            sincos_pos_emb: bool = True,
            learnable_pos_emb: bool = False,
            image_size: Union[int, Tuple[int]] = 224,
        ):

            super().__init__()
            self.num_channels = num_channels
            self.stride_level = stride_level
            self.patch_size_full = pair(patch_size_full)
            self.dim_tokens = dim_tokens
            self.sincos_pos_emb = sincos_pos_emb
            self.learnable_pos_emb = learnable_pos_emb
            self.image_size = pair(image_size)
            self.num_patches = (self.image_size[0] // patch_size_full) * (
                self.image_size[1] // patch_size_full
            )

            # Actual patch height and width, taking into account stride of input
            self.P_H = max(1, self.patch_size_full[0] // stride_level)
            self.P_W = max(1, self.patch_size_full[1] // stride_level)

            if self.dim_tokens is not None:
                self.init(dim_tokens=dim_tokens)

        def init(self, dim_tokens: int = 768):
            """
            Initialize parts of encoder that are dependent on dimension of tokens.
            Should be called when setting up MultiMAE.

            :param dim_tokens: Dimension of tokens
            """
            self.dim_tokens = dim_tokens

            # Task embedding identifying from which task a given token comes from
            # Fixed-size positional embeddings. Can be interpolated to different input sizes
            h_posemb = self.image_size[0] // (self.stride_level * self.P_H)
            w_posemb = self.image_size[1] // (self.stride_level * self.P_W)
            if self.sincos_pos_emb:
                self.pos_emb = build_2d_sincos_posemb(
                    h=h_posemb, w=w_posemb, embed_dim=self.dim_tokens
                )
                self.pos_emb = nn.Parameter(
                    self.pos_emb, requires_grad=self.learnable_pos_emb
                )
            else:
                self.pos_emb = nn.Parameter(
                    torch.zeros(1, self.dim_tokens, h_posemb, w_posemb)
                )
                trunc_normal_(self.pos_emb, std=0.02)

            # Image -> tokens projection
            self.proj = nn.Conv2d(
                in_channels=self.num_channels,
                out_channels=self.dim_tokens,
                kernel_size=(self.P_H, self.P_W),
                stride=(self.P_H, self.P_W),
            )

        @torch.jit.ignore
        def no_weight_decay(self):
            return {"pos_emb"}

        def forward(self, x):
            """
            Forward pass through input adapter, transforming image to sequence of tokens.
            Adds task and positional encodings.

            :param x: Input image tensor
            """
            B, C, H, W = x.shape
            assert (
                self.dim_tokens is not None
            ), "Need to call init(dim_tokens) function first"
            assert (H % self.P_H == 0) and (
                W % self.P_W == 0
            ), f"Image sizes {H}x{W} must be divisible by patch sizes {self.P_H}x{self.P_W}"
            N_H, N_W = (
                H // self.P_H,
                W // self.P_W,
            )  # Number of patches in height and width

            # Create patches [B, C, H, W] -> [B, (H*W), C]
            x_patch = rearrange(self.proj(x), "b d nh nw -> b (nh nw) d")

            # Create positional embedding
            x_pos_emb = F.interpolate(
                self.pos_emb, size=(N_H, N_W), mode="bicubic", align_corners=False
            )
            x_pos_emb = rearrange(x_pos_emb, "b d nh nw -> b (nh nw) d")

            # Add patches and positional embeddings
            x = x_patch + x_pos_emb

            return x

    def drop_path(x, drop_prob: float = 0.0, training: bool = False):
        """Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks).
        This is the same as the DropConnect impl I created for EfficientNet, etc networks, however,
        the original name is misleading as 'Drop Connect' is a different form of dropout in a separate paper...
        See discussion: https://github.com/tensorflow/tpu/issues/494#issuecomment-532968956 ... I've opted for
        changing the layer and argument names to 'drop path' rather than mix DropConnect as a layer name and use
        'survival rate' as the argument.
        """
        if drop_prob == 0.0 or not training:
            return x
        keep_prob = 1 - drop_prob
        shape = (x.shape[0],) + (1,) * (
            x.ndim - 1
        )  # work with diff dim tensors, not just 2D ConvNets
        random_tensor = keep_prob + torch.rand(shape, dtype=x.dtype, device=x.device)
        random_tensor.floor_()  # binarize
        output = x.div(keep_prob) * random_tensor
        return output

    class DropPath(nn.Module):
        """Drop paths (Stochastic Depth) per sample  (when applied in main path of residual blocks)."""

        def __init__(self, drop_prob=None):
            super(DropPath, self).__init__()
            self.drop_prob = drop_prob

        def forward(self, x):
            return drop_path(x, self.drop_prob, self.training)

        def extra_repr(self) -> str:
            return "p={}".format(self.drop_prob)

    class ConvNeXtBlock(nn.Module):
        r"""ConvNeXt Block. There are two equivalent implementations:
        (1) DwConv -> LayerNorm (channels_first) -> 1x1 Conv -> GELU -> 1x1 Conv; all in (N, C, H, W)
        (2) DwConv -> Permute to (N, H, W, C); LayerNorm (channels_last) -> Linear -> GELU -> Linear; Permute back
        We use (2) as we find it slightly faster in PyTorch

        Args:
            dim (int): Number of input channels.
            drop_path: Stochastic depth rate. Default: 0.0
            layer_scale_init_value (float): Init value for Layer Scale. Default: 0 (disabled for isotropic ConvNeXt).

        Code from: https://github.com/facebookresearch/ConvNeXt/blob/main/models/convnext.py
        """

        def __init__(self, dim, drop_path=0.0, layer_scale_init_value=0.0):
            super().__init__()
            self.dwconv = nn.Conv2d(
                dim, dim, kernel_size=7, padding=3, groups=dim
            )  # depthwise conv
            self.norm = nn.LayerNorm(dim, eps=1e-6)
            self.pwconv1 = nn.Linear(
                dim, 4 * dim
            )  # pointwise/1x1 convs, implemented with linear layers
            self.act = nn.GELU()
            self.pwconv2 = nn.Linear(4 * dim, dim)
            self.gamma = (
                nn.Parameter(
                    layer_scale_init_value * torch.ones((dim)), requires_grad=True
                )
                if layer_scale_init_value > 0
                else None
            )
            self.drop_path = DropPath(drop_path) if drop_path > 0.0 else nn.Identity()

        def forward(self, x: Tensor) -> Tensor:
            input = x
            x = self.dwconv(x)
            x = x.permute(0, 2, 3, 1)  # (N, C, H, W) -> (N, H, W, C)
            x = self.norm(x)
            x = self.pwconv1(x)
            x = self.act(x)
            x = self.pwconv2(x)
            if self.gamma is not None:
                x = self.gamma * x
            x = x.permute(0, 3, 1, 2)  # (N, H, W, C) -> (N, C, H, W)

            x = input + self.drop_path(x)
            return x

    class ConvNeXtAdapter(nn.Module):
        """Output adapter with ConvNext blocks for semantic segmentation

        :param num_classes: Number of classes
        :param num_heads: Number of attention heads
        :param embed_dim: Token dimension after projection, and before reshaping operation.
        :param preds_per_patch: Increases size of feature map by reshaping each patch  Each patch gets reshaped
            from embed_dim x 1 x 1 to (embed_dim / preds_per_patch) x (preds_per_patch ** 0.5) x (preds_per_patch ** 0.5)
        :param main_tasks: Tasks to use for the adapter. Only tokens coming from these tasks are kept.
        :param patch_size: Size of patches
        :param depth: Number of ConvNeXt blocks
        :interpolate_mode: Interpolation mode for final upsampling
        """

        def __init__(
            self,
            num_classes,
            embed_dim: int = 6144,
            preds_per_patch: int = 16,
            main_tasks: Iterable[str] = ("rgb",),
            patch_size: int = 16,
            depth: int = 4,
            interpolate_mode: str = "bilinear",
            **kwargs,
        ):
            super().__init__()
            self.main_tasks = main_tasks
            self.patch_size = patch_size
            self.embed_dim = embed_dim
            self.preds_per_patch = preds_per_patch
            self.class_dim = embed_dim // preds_per_patch
            self.num_classes = num_classes
            self.interpolate_mode = interpolate_mode

            self.blocks = nn.Sequential(
                *[ConvNeXtBlock(dim=self.class_dim) for _ in range(depth)]
            )
            self.final_layer = nn.Conv2d(self.class_dim, self.num_classes, 1)
            self.apply(self._init_weights)

        def init(self, dim_tokens_enc: int = 768):
            """
            Initialize parts of decoder that are dependent on dimension of encoder tokens.
            Should be called when setting up MultiMAE.

            :param dim_tokens_enc: Dimension of tokens coming from encoder
            """
            self.in_channels = dim_tokens_enc * len(self.main_tasks)

            # Projection of encoder tokens to the patch dimension
            self.proj_dec = nn.Linear(self.in_channels, self.embed_dim)
            self._init_weights(self.proj_dec)

        def _init_weights(self, m):
            if isinstance(m, nn.Linear):
                trunc_normal_(m.weight, std=0.02)
                if isinstance(m, nn.Linear) and m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.LayerNorm):
                nn.init.constant_(m.bias, 0)
                nn.init.constant_(m.weight, 1.0)

        def adapt_tokens(self, encoder_tokens, input_info):
            # Adapt tokens
            x = []
            for task in self.main_tasks:
                start_idx = input_info["tasks"][task]["start_idx"]
                end_idx = input_info["tasks"][task]["end_idx"]
                x.append(encoder_tokens[:, start_idx:end_idx])

            x = torch.cat(x, dim=-1)
            return x

        def forward(self, encoder_tokens: torch.Tensor, input_info: Dict):
            H, W = input_info["image_size"]
            N_H, N_W = H // self.patch_size, W // self.patch_size

            x = self.adapt_tokens(encoder_tokens, input_info)

            x = self.proj_dec(x)
            x = rearrange(
                x,
                "b n (p c) -> b (n p) c",
                n=N_H * N_W,
                p=self.preds_per_patch,
                c=self.class_dim,
            )
            x = rearrange(
                x,
                "b (nh nw ph pw) c -> b c (nh ph) (nw pw)",
                nh=N_H,
                nw=N_W,
                ph=int(self.preds_per_patch**0.5),
                pw=int(self.preds_per_patch**0.5),
            )

            x = self.blocks(x)

            # for block in self.blocks:
            #     x = block(x)
            #     print(x.shape)

            x = self.final_layer(x)
            # print(x.shape)

            # Interpolate to semseg res
            x = F.interpolate(x, size=(H, W), mode=self.interpolate_mode)

            return x

    def interpolate_pos_embed_multimae(model, checkpoint_model):
        pattern = "input_adapters\.(.*)\.pos_emb"
        matched_keys = [k for k in checkpoint_model if bool(re.match(pattern, k))]

        for key in matched_keys:
            domain = re.match(pattern, key).group(1)  # group(0) is entire matched regex
            if getattr(model.input_adapters, domain, None) is not None:
                pos_embed_checkpoint = checkpoint_model[key]
                _, _, orig_H, orig_W = pos_embed_checkpoint.shape
                _, _, new_H, new_W = getattr(model.input_adapters, domain).pos_emb.shape
                if (orig_H != new_H) or (orig_W != new_W):
                    print(
                        f"Key {key}: Position interpolate from {orig_H}x{orig_W} to {new_H}x{new_W}"
                    )
                    pos_embed_checkpoint = torch.nn.functional.interpolate(
                        pos_embed_checkpoint,
                        size=(new_H, new_W),
                        mode="bicubic",
                        align_corners=False,
                    )
                    checkpoint_model[key] = pos_embed_checkpoint

    class Attention(nn.Module):
        def __init__(
            self,
            dim,
            num_heads=8,
            qkv_bias=False,
            attn_drop=0.0,
            proj_drop=0.0,
        ):
            super().__init__()
            self.num_heads = num_heads
            head_dim = dim // num_heads
            self.scale = head_dim**-0.5

            self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
            self.attn_drop = nn.Dropout(attn_drop)
            self.proj = nn.Linear(dim, dim)
            self.proj_drop = nn.Dropout(proj_drop)

        def forward(self, x):
            B, N, C = x.shape
            qkv = (
                self.qkv(x)
                .reshape(B, N, 3, self.num_heads, C // self.num_heads)
                .permute(2, 0, 3, 1, 4)
            )
            q, k, v = qkv.unbind(
                0
            )  # make torchscript happy (cannot use tensor as tuple)

            attn = (q @ k.transpose(-2, -1)) * self.scale
            attn = attn.softmax(dim=-1)
            attn = self.attn_drop(attn)

            x = (attn @ v).transpose(1, 2).reshape(B, N, C)
            x = self.proj(x)
            x = self.proj_drop(x)
            return x

    class Mlp(nn.Module):
        def __init__(
            self,
            in_features,
            hidden_features=None,
            out_features=None,
            act_layer=nn.GELU,
            drop=0.0,
        ):
            super().__init__()
            out_features = out_features or in_features
            hidden_features = hidden_features or in_features
            self.fc1 = nn.Linear(in_features, hidden_features)
            self.act = act_layer()
            self.fc2 = nn.Linear(hidden_features, out_features)
            self.drop = nn.Dropout(drop)

        def forward(self, x):
            x = self.fc1(x)
            x = self.act(x)
            # x = self.drop(x)
            # commit this for the orignal BERT implement
            x = self.fc2(x)
            x = self.drop(x)
            return x

    class Block(nn.Module):
        def __init__(
            self,
            dim,
            num_heads,
            mlp_ratio=4.0,
            qkv_bias=False,
            drop=0.0,
            attn_drop=0.0,
            drop_path=0.0,
            act_layer=nn.GELU,
            norm_layer=nn.LayerNorm,
        ):
            super().__init__()
            self.norm1 = norm_layer(dim)
            self.attn = Attention(
                dim,
                num_heads=num_heads,
                qkv_bias=qkv_bias,
                attn_drop=attn_drop,
                proj_drop=drop,
            )
            self.drop_path = DropPath(drop_path) if drop_path > 0.0 else nn.Identity()
            self.norm2 = norm_layer(dim)
            mlp_hidden_dim = int(dim * mlp_ratio)
            self.mlp = Mlp(
                in_features=dim,
                hidden_features=mlp_hidden_dim,
                act_layer=act_layer,
                drop=drop,
            )

        def forward(self, x):
            x = x + self.drop_path(self.attn(self.norm1(x)))
            x = x + self.drop_path(self.mlp(self.norm2(x)))
            return x

    class MultiMAE(nn.Module):
        """MultiMAE: Multi-task Multi-modal Masked Autoencoder
        This module performs masking in its forward pass.
        The MultiViT module defined below inherits from this module and performs a regular forward pass,
        and should be used instead for downstream tasks


        :param input_adapters: Dictionary of task -> input adapters
        :param output_adapters: Optional dictionary of task -> output adapters

        :param num_global_tokens: Number of additional global tokens to add (like cls tokens), default is 1
        :param dim_tokens: Dimension of encoder tokens
        :param depth: Depth of encoder
        :param num_heads: Number of attention heads
        :param mlp_ratio: MLP hidden dim ratio
        :param qkv_bias: Set to False to disable bias
        :param drop_rate: Dropout after MLPs and Attention
        :param attn_drop_rate: Attention matrix drop rate
        :param drop_path_rate: DropPath drop rate
        :param norm_layer: Type of normalization layer
        """

        def __init__(
            self,
            input_adapters: Dict[str, nn.Module],
            output_adapters: Optional[Dict[str, nn.Module]],
            num_global_tokens: int = 1,
            dim_tokens: int = 768,
            depth: int = 12,
            num_heads: int = 12,
            mlp_ratio: float = 4.0,
            qkv_bias: bool = True,
            drop_rate: float = 0.0,
            attn_drop_rate: float = 0.0,
            drop_path_rate: float = 0.0,
            norm_layer: nn.Module = partial(nn.LayerNorm, eps=1e-6),
        ):
            super().__init__()

            # Initialize input and output adapters
            for adapter in input_adapters.values():
                adapter.init(dim_tokens=dim_tokens)
            self.input_adapters = nn.ModuleDict(input_adapters)
            if output_adapters is not None:
                for adapter in output_adapters.values():
                    adapter.init(dim_tokens_enc=dim_tokens)
                self.output_adapters = nn.ModuleDict(output_adapters)
            else:
                self.output_adapters = None

            # Additional learnable tokens that can be used by encoder to process/store global information
            self.num_global_tokens = num_global_tokens
            self.global_tokens = nn.Parameter(
                torch.zeros(1, num_global_tokens, dim_tokens)
            )
            trunc_normal_(self.global_tokens, std=0.02)

            # Transformer encoder
            dpr = [
                x.item() for x in torch.linspace(0, drop_path_rate, depth)
            ]  # stochastic depth decay rule
            self.encoder = nn.Sequential(
                *[
                    Block(
                        dim=dim_tokens,
                        num_heads=num_heads,
                        mlp_ratio=mlp_ratio,
                        qkv_bias=qkv_bias,
                        drop=drop_rate,
                        attn_drop=attn_drop_rate,
                        drop_path=dpr[i],
                        norm_layer=norm_layer,
                    )
                    for i in range(depth)
                ]
            )

            self.apply(self._init_weights)
            for name, m in self.named_modules():
                if isinstance(m, nn.Linear):
                    if "qkv" in name:
                        # treat the weights of Q, K, V separately
                        val = math.sqrt(
                            6.0 / float(m.weight.shape[0] // 3 + m.weight.shape[1])
                        )
                        nn.init.uniform_(m.weight, -val, val)
                    elif "kv" in name:
                        # treat the weights of K, V separately
                        val = math.sqrt(
                            6.0 / float(m.weight.shape[0] // 2 + m.weight.shape[1])
                        )
                        nn.init.uniform_(m.weight, -val, val)

                if isinstance(m, nn.Conv2d):
                    if ".proj" in name:
                        # From MAE, initialize projection like nn.Linear (instead of nn.Conv2d)
                        w = m.weight.data
                        nn.init.xavier_uniform_(w.view([w.shape[0], -1]))

        def _init_weights(self, m):
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if isinstance(m, nn.Linear) and m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.LayerNorm):
                nn.init.constant_(m.bias, 0)
                nn.init.constant_(m.weight, 1.0)

        def get_num_layers(self):
            return len(self.encoder)

        @torch.jit.ignore
        def no_weight_decay(self):
            no_wd_set = {"global_tokens"}

            for task, adapter in self.input_adapters.items():
                if hasattr(adapter, "no_weight_decay"):
                    to_skip = adapter.no_weight_decay()
                    to_skip = set([f"input_adapters.{task}.{name}" for name in to_skip])
                    no_wd_set = no_wd_set | to_skip

            for task, adapter in self.output_adapters.items():
                if hasattr(adapter, "no_weight_decay"):
                    to_skip = adapter.no_weight_decay()
                    to_skip = set(
                        [f"output_adapters.{task}.{name}" for name in to_skip]
                    )
                    no_wd_set = no_wd_set | to_skip

            return no_wd_set

        def generate_input_info(self, input_task_tokens, image_size):
            input_info = OrderedDict()
            i = 0
            input_info["tasks"] = {}
            for domain, tensor in input_task_tokens.items():
                num_tokens = tensor.shape[1]
                d = {
                    "num_tokens": num_tokens,
                    "has_2d_posemb": True,  # TODO: Modify when adding non-2D tasks
                    "start_idx": i,
                    "end_idx": i + num_tokens,
                }
                i += num_tokens
                input_info["tasks"][domain] = d

            input_info["image_size"] = image_size
            input_info["num_task_tokens"] = i
            input_info["num_global_tokens"] = self.num_global_tokens

            return input_info

    class MultiViT(MultiMAE):
        """MultiViT: Multi-modal Vision Transformer
        This is MultiMAE without masking and with a simplified / faster forward pass


        :param input_adapters: Dictionary of task -> input adapters
        :param output_adapters: Optional dictionary of task -> output adapters

        :param num_global_tokens: Number of additional global tokens to add (like cls tokens), default is 1
        :param dim_tokens: Dimension of encoder tokens
        :param depth: Depth of encoder
        :param num_heads: Number of attention heads
        :param mlp_ratio: MLP hidden dim ratio
        :param qkv_bias: Set to False to disable bias
        :param drop_rate: Dropout after MLPs and Attention
        :param attn_drop_rate: Attention matrix drop rate
        :param drop_path_rate: DropPath drop rate
        :param norm_layer: Type of normalization layer
        """

        def process_input(self, x):

            # If input x is a Tensor, assume it's RGB
            x = {"rgb": x} if isinstance(x, torch.Tensor) else x
            # Need image size for tokens->image reconstruction
            if "rgb" in x:
                B, _, H, W = x["rgb"].shape
            elif "semseg" in x:
                B, H, W = x["semseg"].shape
                H *= self.input_adapters["semseg"].stride_level
                W *= self.input_adapters["semseg"].stride_level
            else:
                B, _, H, W = list(x.values())[
                    0
                ].shape  # TODO: Deal with case where not all have same shape

            # Encode selected inputs to tokens
            input_task_tokens = {
                domain: self.input_adapters[domain](tensor)
                for domain, tensor in x.items()
                if domain in self.input_adapters
            }

            input_info = self.generate_input_info(
                input_task_tokens=input_task_tokens, image_size=(H, W)
            )
            input_tokens = torch.cat(
                [task_tokens for task_tokens in input_task_tokens.values()], dim=1
            )

            # Add global tokens to input tokens
            global_tokens = repeat(self.global_tokens, "() n d -> b n d", b=B)
            input_tokens = torch.cat([input_tokens, global_tokens], dim=1)

            return input_tokens, input_info

        def forward(
            self,
            x: Union[Dict[str, torch.Tensor], torch.Tensor],
            return_all_layers=False,
            **kwargs,
        ):
            """
            Forward pass through input adapters, transformer encoder and output adapters.

            :param x: Input tensor or dictionary of tensors
            :param return_all_layers: Set to True to return all transformer layers
            """

            input_tokens, input_info = self.process_input(x)

            # Pass tokens through Transformer
            if not return_all_layers:
                encoder_tokens = self.encoder(input_tokens)
            else:
                # Optionally access every intermediate layer
                encoder_tokens = []
                tokens = input_tokens
                for block in self.encoder:
                    tokens = block(tokens)
                    encoder_tokens.append(tokens)

            if self.output_adapters is None:
                return encoder_tokens

            # Decode tokens for each task using task-specific output adapters
            preds = {
                domain: self.output_adapters[domain](
                    encoder_tokens=encoder_tokens,
                    input_info=input_info,
                )
                for domain in self.output_adapters
            }

            return preds

    class args:
        patch_size = 16
        input_size = image_size
        learnable_pos_emb = False
        decoder_interpolate_mode = "bilinear"  # ['bilinear', 'nearest']
        finetune = "./pretrained_weights/multimae-b_98_rgb+-depth-semseg_1600e_multivit-afff3f8c.pth"
        finetune = (
            os.path.join(
                cfg.source_code_dir,
                "backbones",
                "multimae",
                "multimae-b_98_rgb+-depth-semseg_1600e_multivit-afff3f8c.pth",
            )
            if is_colab
            else "./pretrained_weights/multimae-b_98_rgb+-depth-semseg_1600e_multivit-afff3f8c.pth"
        )

    input_adapters = {
        "rgb": PatchedInputAdapter(
            num_channels=3,
            stride_level=1,
            patch_size_full=args.patch_size,
            image_size=args.input_size,
            learnable_pos_emb=args.learnable_pos_emb,
        ),
        "depth": PatchedInputAdapter(
            num_channels=1,
            stride_level=1,
            patch_size_full=args.patch_size,
            image_size=args.input_size,
            learnable_pos_emb=args.learnable_pos_emb,
        ),
    }

    output_adapters = {
        "semseg": ConvNeXtAdapter(
            num_classes=1,
            embed_dim=6144,
            patch_size=16,
            preds_per_patch=16,
            depth=4,
            interpolate_mode=args.decoder_interpolate_mode,
            main_tasks=["rgb"],
        ),
    }

    model = MultiViT(
        input_adapters=input_adapters,
        output_adapters=output_adapters,
        drop_path_rate=0.1,
        dim_tokens=768,
        depth=12,
        num_heads=12,
        mlp_ratio=4,
        qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
    )

    untrained_keys: List[str] = []

    if args.finetune:
        checkpoint = torch.load(args.finetune, map_location="cpu")

        checkpoint_model = checkpoint["model"]

        class_emb_key = "input_adapters.semseg.class_emb.weight"
        if class_emb_key in checkpoint_model:
            checkpoint_model[class_emb_key] = F.pad(
                checkpoint_model[class_emb_key], (0, 0, 0, 1)
            )

        # Remove output adapters
        for k in list(checkpoint_model.keys()):
            if "output_adapters" in k:
                del checkpoint_model[k]

        # Interpolate position embedding
        interpolate_pos_embed_multimae(model, checkpoint_model)

        # Load pre-trained model
        msg = model.load_state_dict(checkpoint_model, strict=False)
        print(msg)

        untrained_keys = msg.missing_keys + msg.unexpected_keys

    opt_params = []
    for n, p in model.named_parameters():
        if n in untrained_keys:
            opt_params.append({"params": p, "name": n, "lr_scale": lr_scale})
        else:
            opt_params.append({"params": p, "name": n, "lr_scale": 1.0})

    return model, opt_params


def main():
    model, opt_params = MULTIMAE(lr_scale=10, image_size=352, is_colab=False)

    # model.cuda()

    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)

    # print("Model = %s" % str(model))
    print("number of params: {} M".format(n_parameters / 1e6))

    rgbs = torch.randn((1, 3, 352, 352))
    depths = torch.randn((1, 1, 352, 352))
    outs = model.forward({"rgb": rgbs, "depth": depths})
    print(outs["semseg"].shape)


if __name__ == "__main__":
    main()
