# https://github.com/openai/CLIP/tree/main/clip

import hashlib
import os
import urllib
import requests
import warnings
import time
from typing import Any, Union, List, Tuple
from collections import OrderedDict
from pkg_resources import packaging

from PIL import Image
from tqdm import tqdm

import torch
from torch import nn
import numpy as np
from torchvision.transforms import Compose, Resize, CenterCrop, ToTensor, Normalize
import torch.nn.functional as F

import html
from functools import lru_cache
import ftfy
import regex as re


try:
    from torchvision.transforms import InterpolationMode
    BICUBIC = InterpolationMode.BICUBIC
except ImportError:
    BICUBIC = Image.BICUBIC

# torch.set_default_tensor_type('torch.cuda.HalfTensor')
torch.set_default_tensor_type('torch.cuda.FloatTensor')

_MODELS = {
  "RN50": "https://openaipublic.azureedge.net/clip/models/afeb0e10f9e5a86da6080e35cf09123aca3b358a0c3e3b6c78a7b63bc04b6762/RN50.pt",
  "RN101": "https://openaipublic.azureedge.net/clip/models/8fa8567bab74a42d41c5915025a8e4538c3bdbe8804a470a72f30b0d94fab599/RN101.pt",
  "RN50x4": "https://openaipublic.azureedge.net/clip/models/7e526bd135e493cef0776de27d5f42653e6b4c8bf9e0f653bb11773263205fdd/RN50x4.pt",
  "RN50x16": "https://openaipublic.azureedge.net/clip/models/52378b407f34354e150460fe41077663dd5b39c54cd0bfd2b27167a4a06ec9aa/RN50x16.pt",
  "RN50x64": "https://openaipublic.azureedge.net/clip/models/be1cfb55d75a9666199fb2206c106743da0f6468c9d327f3e0d0a543a9919d9c/RN50x64.pt",
  "ViT-B/32": "https://openaipublic.azureedge.net/clip/models/40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af/ViT-B-32.pt",
  "ViT-B/16": "https://openaipublic.azureedge.net/clip/models/5806e77cd80f8b59890b7e101eabd078d9fb84e6937f9e85e4ecb61988df416f/ViT-B-16.pt",
  "ViT-L/14": "https://openaipublic.azureedge.net/clip/models/b8cca3fd41ae0c99ba7e8951adf17d267cdb84cd88be6f7c2e0eca1737a03836/ViT-L-14.pt",
  "ViT-L/14@336px": "https://openaipublic.azureedge.net/clip/models/3035c92b350959924f9f00213499208652fc7ea050643e8b385c2dac08641f02/ViT-L-14-336px.pt",
}

# utils

#import gzip


# tokenizer

@lru_cache()
def default_bpe():
  return 

@lru_cache()
def bytes_to_unicode():
  bs = list(range(ord("!"), ord("~")+1))+list(range(ord("¡"), ord("¬")+1))+list(range(ord("®"), ord("ÿ")+1))
  cs = bs[:]
  n = 0
  for b in range(2**8):
    if b not in bs:
      bs.append(b)
      cs.append(2**8+n)
      n += 1
  cs = [chr(n) for n in cs]
  return dict(zip(bs, cs))

def get_pairs(word):
  pairs = set()
  prev_char = word[0]
  for char in word[1:]:
    pairs.add((prev_char, char))
    prev_char = char
  return pairs

def basic_clean(text):
  text = ftfy.fix_text(text)
  text = html.unescape(html.unescape(text))
  return text.strip()

def whitespace_clean(text):
  text = re.sub(r'\s+', ' ', text)
  text = text.strip()
  return text

class SimpleTokenizer(object):
  def __init__(self, bpe_path: str = "bpe_simple_vocab_16e6.txt"):
    self.byte_encoder = bytes_to_unicode()
    self.byte_decoder = {v: k for k, v in self.byte_encoder.items()}
    with open(bpe_path, 'r', encoding='utf-8') as file:
      merges = file.read().split('\n')
    merges = merges[1:49152-256-2+1]
    merges = [tuple(merge.split()) for merge in merges]
    vocab = list(bytes_to_unicode().values())
    vocab = vocab + [v+'</w>' for v in vocab]
    for merge in merges:
      vocab.append(''.join(merge))
    vocab.extend(['<|startoftext|>', '<|endoftext|>'])
    self.encoder = dict(zip(vocab, range(len(vocab))))
    self.decoder = {v: k for k, v in self.encoder.items()}
    self.bpe_ranks = dict(zip(merges, range(len(merges))))
    self.cache = {'<|startoftext|>': '<|startoftext|>', '<|endoftext|>': '<|endoftext|>'}
    self.pat = re.compile(r"""<\|startoftext\|>|<\|endoftext\|>|'s|'t|'re|'ve|'m|'ll|'d|[\p{L}]+|[\p{N}]|[^\s\p{L}\p{N}]+""", re.IGNORECASE)

  def bpe(self, token):
    if token in self.cache:
      return self.cache[token]
    word = tuple(token[:-1]) + ( token[-1] + '</w>',)
    pairs = get_pairs(word)

    if not pairs:
      return token+'</w>'

    while True:
      bigram = min(pairs, key = lambda pair: self.bpe_ranks.get(pair, float('inf')))
      if bigram not in self.bpe_ranks:
        break
      first, second = bigram
      new_word = []
      i = 0
      while i < len(word):
        try:
          j = word.index(first, i)
          new_word.extend(word[i:j])
          i = j
        except:
          new_word.extend(word[i:])
          break

        if word[i] == first and i < len(word)-1 and word[i+1] == second:
          new_word.append(first+second)
          i += 2
        else:
          new_word.append(word[i])
          i += 1
      new_word = tuple(new_word)
      word = new_word
      if len(word) == 1:
        break
      else:
        pairs = get_pairs(word)
    word = ' '.join(word)
    self.cache[token] = word
    return word

  def encode(self, text):
    bpe_tokens = []
    text = whitespace_clean(basic_clean(text)).lower()
    for token in re.findall(self.pat, text):
      token = ''.join(self.byte_encoder[b] for b in token.encode('utf-8'))
      bpe_tokens.extend(self.encoder[bpe_token] for bpe_token in self.bpe(token).split(' '))
    return bpe_tokens

  def decode(self, tokens):
    text = ''.join([self.decoder[token] for token in tokens])
    text = bytearray([self.byte_decoder[c] for c in text]).decode('utf-8', errors="replace").replace('</w>', ' ')
    return text

_tokenizer = SimpleTokenizer()

def tokenize(texts: Union[str, List[str]], context_length: int = 77, truncate: bool = False) -> Union[torch.IntTensor, torch.LongTensor]:
    if isinstance(texts, str):
        texts = [texts]

    sot_token = _tokenizer.encoder["<|startoftext|>"]
    eot_token = _tokenizer.encoder["<|endoftext|>"]
    all_tokens = [[sot_token] + _tokenizer.encode(text) + [eot_token] for text in texts]
    if packaging.version.parse(torch.__version__) < packaging.version.parse("1.8.0"):
        result = torch.zeros(len(all_tokens), context_length, dtype=torch.long)
    else:
        result = torch.zeros(len(all_tokens), context_length, dtype=torch.int)

    for i, tokens in enumerate(all_tokens):
        if len(tokens) > context_length:
            if truncate:
                tokens = tokens[:context_length]
                tokens[-1] = eot_token
            else:
                raise RuntimeError(f"Input {texts[i]} is too long for context length {context_length}")
        result[i, :len(tokens)] = torch.tensor(tokens)

    return result

# clip model architecture

class LayerNorm(nn.LayerNorm):
  def forward(self, x: torch.Tensor):
    orig_type = x.dtype
    ret = super().forward(x.type(torch.float32))
    return ret.type(orig_type)

class QuickGELU(nn.Module):
  def forward(self, x: torch.Tensor):
    return x * torch.sigmoid(1.702 * x)

class ResidualAttentionBlock(nn.Module):
  def __init__(self, d_model: int, n_head: int, attn_mask: torch.Tensor = None):
    super().__init__()
    self.attn = nn.MultiheadAttention(d_model, n_head)
    self.ln_1 = LayerNorm(d_model)
    self.mlp = nn.Sequential(OrderedDict([
      ("c_fc", nn.Linear(d_model, d_model * 4)),
      ("gelu", QuickGELU()),
      ("c_proj", nn.Linear(d_model * 4, d_model))
    ]))
    self.ln_2 = LayerNorm(d_model)
    self.attn_mask = attn_mask

  def attention(self, x: torch.Tensor):
    self.attn_mask = self.attn_mask.to(dtype=x.dtype, device=x.device) if self.attn_mask is not None else None
    return self.attn(x, x, x, need_weights=False, attn_mask=self.attn_mask)[0]

  def forward(self, x: torch.Tensor):
    x = x + self.attention(self.ln_1(x))
    x = x + self.mlp(self.ln_2(x))
    return x

class ModifiedResNet(nn.Module):
  def __init__(self, layers, output_dim, heads, input_resolution=224, width=64):
    super().__init__()

class Transformer(nn.Module):
  def __init__(self, width: int, layers: int, heads: int, attn_mask: torch.Tensor = None):
    super().__init__()
    self.width = width
    self.layers = layers
    self.resblocks = nn.Sequential(*[ResidualAttentionBlock(width, heads, attn_mask) for _ in range(layers)])

  def forward(self, x: torch.Tensor):
    return self.resblocks(x)

class VisionTransformer(nn.Module):
  def __init__(self, input_resolution: int, patch_size: int, width: int, layers: int, heads: int, output_dim: int):
    super().__init__()
    self.input_resolution = input_resolution
    self.output_dim = output_dim
    self.conv1 = nn.Conv2d(in_channels=3, out_channels=width, kernel_size=patch_size, stride=patch_size, bias=False)

    scale = width ** -0.5
    self.class_embedding = nn.Parameter(scale * torch.randn(width))
    self.positional_embedding = nn.Parameter(scale * torch.randn((input_resolution // patch_size) ** 2 + 1, width))
    self.ln_pre = LayerNorm(width)

    self.transformer = Transformer(width, layers, heads)

    self.ln_post = LayerNorm(width)
    self.proj = nn.Parameter(scale * torch.randn(width, output_dim))

  def forward(self, x: torch.Tensor):
    # print("VisionTransformer.forward")
    # print(f"\t x (input): ", x.shape)
    x = self.conv1(x)  # shape = [*, width, grid, grid]
    # print(f"\t post conv1: ", x.shape)
    x = x.reshape(x.shape[0], x.shape[1], -1)  # shape = [*, width, grid ** 2]
    # print(f"\t post reshape: ", x.shape)
    x = x.permute(0, 2, 1)  # shape = [*, grid ** 2, width]
    # print(f"\t post permute: ", x.shape)
    x = torch.cat([self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device), x], dim=1)  # shape = [*, grid ** 2 + 1, width]
    x = x + self.positional_embedding.to(x.dtype)
    # print(f"\t post positional embed: ", x.shape)
    x = self.ln_pre(x)
    # print(f"\t post ln_pre: ", x.shape)

    x = x.permute(1, 0, 2)  # NLD -> LND
    # print(f"\t post NLD -> LND, pre transformer: ", x.shape)
    x = self.transformer(x)
    # print(f"\t post transformer: ", x.shape)
    x = x.permute(1, 0, 2)  # LND -> NLD
    # print(f"\t post LND -> NLD: ", x.shape)
    # return x
    x = self.ln_post(x[:, 0, :])
    # print(f"\t ln_post: ", x.shape)

    if self.proj is not None:
      x = x @ self.proj
      # print(f"\t proj: ", x.shape)
    # print(f"\t x (output): ", x.shape)
    return x

class CLIP(nn.Module):
  def __init__(self,
          embed_dim: int,
          # vision
          image_resolution: int,
          vision_layers: Union[Tuple[int, int, int, int], int],
          vision_width: int,
          vision_patch_size: int,
          # text
          context_length: int,
          vocab_size: int,
          transformer_width: int,
          transformer_heads: int,
          transformer_layers: int
          ):
    super().__init__()
    self.context_length = context_length

    if isinstance(vision_layers, (tuple, list)):
      vision_heads = vision_width * 32 // 64
      raise RuntimeError(f"CLIP.init ResNet Error")
      # self.visual = ModifiedResNet(
      #   layers=vision_layers,
      #   output_dim=embed_dim,
      #   heads=vision_heads,
      #   input_resolution=image_resolution,
      #   width=vision_width
      # )
    else:
      vision_heads = vision_width // 64
      self.visual = VisionTransformer(
        input_resolution=image_resolution,
        patch_size=vision_patch_size,
        width=vision_width,
        layers=vision_layers,
        heads=vision_heads,
        output_dim=embed_dim
      )

    self.transformer = Transformer(
      width=transformer_width,
      layers=transformer_layers,
      heads=transformer_heads,
      attn_mask=self.build_attention_mask()
    )

    self.vocab_size = vocab_size
    self.token_embedding = nn.Embedding(vocab_size, transformer_width)
    self.positional_embedding = nn.Parameter(torch.empty(self.context_length, transformer_width))
    self.ln_final = LayerNorm(transformer_width)

    self.text_projection = nn.Parameter(torch.empty(transformer_width, embed_dim))
    self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))

    self.initialize_parameters()

  def initialize_parameters(self):
    nn.init.normal_(self.token_embedding.weight, std=0.02)
    nn.init.normal_(self.positional_embedding, std=0.01)

    if isinstance(self.visual, ModifiedResNet):
      raise RuntimeError(f"CLIP.initialize_parameters ResNet Error")
      # if self.visual.attnpool is not None:
      #   std = self.visual.attnpool.c_proj.in_features ** -0.5
      #   nn.init.normal_(self.visual.attnpool.q_proj.weight, std=std)
      #   nn.init.normal_(self.visual.attnpool.k_proj.weight, std=std)
      #   nn.init.normal_(self.visual.attnpool.v_proj.weight, std=std)
      #   nn.init.normal_(self.visual.attnpool.c_proj.weight, std=std)

      # for resnet_block in [self.visual.layer1, self.visual.layer2, self.visual.layer3, self.visual.layer4]:
      #   for name, param in resnet_block.named_parameters():
      #     if name.endswith("bn3.weight"):
      #       nn.init.zeros_(param)

    proj_std = (self.transformer.width ** -0.5) * ((2 * self.transformer.layers) ** -0.5)
    attn_std = self.transformer.width ** -0.5
    fc_std = (2 * self.transformer.width) ** -0.5
    for block in self.transformer.resblocks:
      nn.init.normal_(block.attn.in_proj_weight, std=attn_std)
      nn.init.normal_(block.attn.out_proj.weight, std=proj_std)
      nn.init.normal_(block.mlp.c_fc.weight, std=fc_std)
      nn.init.normal_(block.mlp.c_proj.weight, std=proj_std)

    if self.text_projection is not None:
      nn.init.normal_(self.text_projection, std=self.transformer.width ** -0.5)

  def build_attention_mask(self):
    # lazily create causal attention mask, with full attention between the vision tokens
    # pytorch uses additive attention mask; fill with -inf
    mask = torch.empty(self.context_length, self.context_length)
    mask.fill_(float("-inf"))
    mask.triu_(1)  # zero out the lower diagonal
    return mask

  @property
  def dtype(self):
    return self.visual.conv1.weight.dtype

  def encode_image(self, image):
    return self.visual(image.type(self.dtype))

  def encode_text(self, text):
    x = self.token_embedding(text).type(self.dtype)  # [batch_size, n_ctx, d_model]

    x = x + self.positional_embedding.type(self.dtype)
    x = x.permute(1, 0, 2)  # NLD -> LND
    x = self.transformer(x)
    x = x.permute(1, 0, 2)  # LND -> NLD
    x = self.ln_final(x).type(self.dtype)

    # x.shape = [batch_size, n_ctx, transformer.width]
    # take features from the eot embedding (eot_token is the highest number in each sequence)
    x = x[torch.arange(x.shape[0]), text.argmax(dim=-1)] @ self.text_projection

    return x
  
  def forward(self, image, text):
    print(f"CLIP.forward")
    image_features = self.encode_image(image)
    text_features = self.encode_text(text)

    # normalized features
    image_features = image_features / image_features.norm(dim=1, keepdim=True)
    text_features = text_features / text_features.norm(dim=1, keepdim=True)

    # cosine similarity as logits
    logit_scale = self.logit_scale.exp()
    logits_per_image = logit_scale * image_features @ text_features.t()
    logits_per_text = logits_per_image.t()

    # shape = [global_batch_size, global_batch_size]
    return logits_per_image, logits_per_text

# model utils

def convert_weights(model: nn.Module):
  # Convert applicable model parameters to fp16

  def _convert_weights_to_fp16(l):
    if isinstance(l, (nn.Conv1d, nn.Conv2d, nn.Linear)):
      l.weight.data = l.weight.data.half()
      if l.bias is not None:
        l.bias.data = l.bias.data.half()

    if isinstance(l, nn.MultiheadAttention):
      for attr in [*[f"{s}_proj_weight" for s in ["in", "q", "k", "v"]], "in_proj_bias", "bias_k", "bias_v"]:
        tensor = getattr(l, attr)
        if tensor is not None:
            tensor.data = tensor.data.half()

    for name in ["text_projection", "proj"]:
      if hasattr(l, name):
        attr = getattr(l, name)
        if attr is not None:
           attr.data = attr.data.half()

  model.apply(_convert_weights_to_fp16)

def build_model(state_dict: dict):
  print(f"build model")
  start_time = time.time()
  vision_width = state_dict["visual.conv1.weight"].shape[0]
  vision_layers = len([k for k in state_dict.keys() if k.startswith("visual.") and k.endswith(".attn.in_proj_weight")])
  vision_patch_size = state_dict["visual.conv1.weight"].shape[-1]
  grid_size = round((state_dict["visual.positional_embedding"].shape[0] - 1) ** 0.5)
  image_resolution = vision_patch_size * grid_size

  embed_dim = state_dict["text_projection"].shape[1]
  context_length = state_dict["positional_embedding"].shape[0]
  vocab_size = state_dict["token_embedding.weight"].shape[0]
  transformer_width = state_dict["ln_final.weight"].shape[0]
  transformer_heads = transformer_width // 64
  transformer_layers = len(set(k.split(".")[2] for k in state_dict if k.startswith("transformer.resblocks")))

  model = CLIP(
    embed_dim,
    image_resolution, vision_layers, vision_width, vision_patch_size,
    context_length, vocab_size, transformer_width, transformer_heads, transformer_layers
  )

  for key in ["input_resolution", "context_length", "vocab_size"]:
    if key in state_dict:
      del state_dict[key]
  print(f"\tconverting weights")
  convert_weights(model)
  print(f"\tloading state_dict")
  model.load_state_dict(state_dict, strict=False)
  print(f"\tbuilt in {time.time() - start_time:.2f} seconds")
  print(f"\tmemory alloc: {torch.cuda.memory_allocated(0) / 1}")
  print("\tmodel params:", f"{np.sum([int(np.prod(p.shape)) for p in model.parameters()]):,}")
  return model.eval()

def _download(url: str, root: str = '.'):
  os.makedirs(root, exist_ok=True)
  filename = os.path.basename(url)
  expected_sha256 = url.split("/")[-2]
  download_target = os.path.join(root, filename)

  if os.path.exists(download_target) and not os.path.isfile(download_target):
    raise RuntimeError(f"{download_target} exists and is not a regular file")

  if os.path.isfile(download_target):
    if hashlib.sha256(open(download_target, "rb").read()).hexdigest() == expected_sha256:
      return download_target
    else:
      warnings.warn(f"{download_target} exists, but the SHA256 checksum does not match; re-downloading the file")

  with urllib.request.urlopen(url) as source, open(download_target, "wb") as output:
    with tqdm(total=int(source.info().get("Content-Length")), ncols=80, unit='iB', unit_scale=True, unit_divisor=1024) as loop:
      while True:
        buffer = source.read(8192)
        if not buffer:
          break

        output.write(buffer)
        loop.update(len(buffer))

  if hashlib.sha256(open(download_target, "rb").read()).hexdigest() != expected_sha256:
    raise RuntimeError("Model has been downloaded but the SHA256 checksum does not not match")

  return download_target

def _convert_image_to_rgb(image):
  return image.convert("RGB")

def _transform(n_px):
  return Compose([
    Resize(n_px, interpolation=BICUBIC),
    CenterCrop(n_px),
    _convert_image_to_rgb,
    ToTensor(),
    Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711)),
  ])

def load(name: str, device: Union[str, torch.device] = "cuda" if torch.cuda.is_available() else "cpu", jit: bool = False, download_root: str = None):
  if name in _MODELS:
    model_path = _download(_MODELS[name])
  elif os.path.isfile(name):
    model_path = name
  else:
    raise RuntimeError(f"Model {name} not found; available models = {available_models()}")

  print(f"load model")
  with open(model_path, 'rb') as opened_file:
    try:
      # loading JIT archive
      model = torch.jit.load(opened_file, map_location=device if jit else "cpu").eval()
      state_dict = None
    except RuntimeError:
      # loading saved state dict
      if jit:
        warnings.warn(f"\tFile {model_path} is not a JIT archive. Loading as a state dict instead")
        jit = False
      state_dict = torch.load(opened_file, map_location="cpu")

  if not jit:
    print(f"\t loading model directly from state_dict (not jit)")
    model = build_model(state_dict or model.state_dict()).to(device)
    if str(device) == "cpu":
      model.float()
    return model, _transform(model.visual.input_resolution)

def test_model():
  model, preprocess = load("ViT-L/14@336px")
  model.cuda().eval()

  url = "http://images.cocodataset.org/val2017/000000039769.jpg"
  image = Image.open(requests.get(url, stream=True).raw)
  image = preprocess(image).unsqueeze(0).to("cuda")
  text = [
    "two cats sleeping", 
    "cat sleeping", 
    "sand falling from an airplane over a lake",
  ]
  text = tokenize(text).cuda()
  with torch.no_grad():
    logits_per_image, _ = model(image, text)
    probs = logits_per_image.softmax(dim=-1).cpu().numpy()

  print(logits_per_image)
  print(probs)
