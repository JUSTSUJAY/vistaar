import os
import argparse
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, AutoConfig
from transformers import pipeline
import evaluate
from joblib import Parallel, delayed
from tqdm import tqdm
import json
import librosa
import pandas as pd
from datasets import Dataset, load_dataset, DatasetDict, Audio
from indicnlp.normalize.indic_normalize import IndicNormalizerFactory
import pyarrow as pa
import soundfile as sf
import jiwer
import os
import string
import re
import time
import deepspeed
from deepspeed import module_inject
import torch
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import sys

from transformers import (
    AutoConfig,
    AutoFeatureExtractor,
    AutoModelForSpeechSeq2Seq,
    AutoProcessor,
    AutoTokenizer,
    HfArgumentParser,
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
    TrainerCallback,
    set_seed,
)

lang_codes = {
    'Hindi': 'hi',
    'Sanskrit': 'sa',
    'Bengali': 'bn',
    'Tamil': 'ta',
    'Telugu': 'te',
    'Gujarati': 'gu',
    'Kannada': 'kn',
    'Malayalam': 'ml',
    'Marathi': 'mr',
    'Odia': 'or',
    'Punjabi': 'pa',
    'Urdu': 'ur',
}

def normalize_sentence(sentence, lang_code):
    '''
    Perform NFC -> NFD normalization for a sentence and a given language
    sentence: string
    lang_code: language code in ISO format
    '''
    factory=IndicNormalizerFactory()
    normalizer=factory.get_normalizer(lang_code)
    normalized_sentence = normalizer.normalize(sentence)
    return normalized_sentence

def compute_transition_scores(
    vocab_size: int,
    sequences: torch.Tensor,
    scores: Tuple[torch.Tensor],
    beam_indices: Optional[torch.Tensor] = None,
    normalize_logits: bool = False,
) -> torch.Tensor:

    # 1. In absence of `beam_indices`, we can assume that we come from e.g. greedy search, which is equivalent
    # to a beam search approach were the first (and only) beam is always selected
    if beam_indices is None:
        beam_indices = torch.arange(scores[0].shape[0]).view(-1, 1).to(sequences.device)
        beam_indices = beam_indices.expand(-1, len(scores))

    # 2. reshape scores as [batch_size*vocab_size, # generation steps] with # generation steps being
    # seq_len - input_length
    scores = torch.stack(scores).reshape(len(scores), -1).transpose(0, 1)

    # 3. Optionally normalize the logits (across the vocab dimension)
    if normalize_logits:
        scores = scores.reshape(-1, vocab_size, scores.shape[-1])
        scores = torch.nn.functional.log_softmax(scores, dim=1)
        scores = scores.reshape(-1, scores.shape[-1])

    # 4. cut beam_indices to longest beam length
    beam_indices_mask = beam_indices < 0
    max_beam_length = (1 - beam_indices_mask.long()).sum(-1).max()
    beam_indices = beam_indices.clone()[:, :max_beam_length]
    beam_indices_mask = beam_indices_mask[:, :max_beam_length]

    # 5. Set indices of beams that finished early to 0; such indices will be masked correctly afterwards
    beam_indices[beam_indices_mask] = 0

    # 6. multiply beam_indices with vocab size to gather correctly from scores
    beam_sequence_indices = beam_indices * vocab_size

    # 7. Define which indices contributed to scores
    cut_idx = sequences.shape[-1] - max_beam_length
    indices = sequences[:, cut_idx:] + beam_sequence_indices

    # 8. Compute scores
    transition_scores = scores.gather(0, indices)

    # 9. Mask out transition_scores of beams that stopped early
    transition_scores[beam_indices_mask] = 0

    return transition_scores

def map_to_pred(batch):
    
    arrays = []
    for aud in batch['audio']:
        arrays.append(aud['array'])

    input_values = processor(arrays, return_tensors="pt", sampling_rate=16_000).input_features.half().to(f'cuda:{local_rank}')
    forced_decoder_ids = processor.get_decoder_prompt_ids(language=lang_code, task="transcribe")
    with torch.no_grad():
        predicted_ids = model.generate(input_values, forced_decoder_ids=forced_decoder_ids, return_dict_in_generate=True, output_scores=True, renormalize_logits = True)

    batch_size = predicted_ids.scores[0].shape[0]
    
    batch['scores'] = compute_transition_scores(model.config.vocab_size, predicted_ids.sequences, predicted_ids.scores, normalize_logits = True)
    batch['scores'] = batch['scores'].tolist()

    transcription = processor.tokenizer.batch_decode(predicted_ids.sequences, skip_special_tokens=True, normalize=False)

    batch["pred_text"] = transcription

    with open(output_path, 'a') as f:
        for i in range(batch_size):
            resp = {
                'audio_filepath' : batch['audio_filepath'][i],
                'duration': batch['duration'][i],
                'pred_text' : batch['pred_text'][i],
                'scores' : batch['scores'][i]
            }
            json.dump(resp, f)
            f.write('\n')

def get_duration(batch):
    try:
        batch['duration'] = librosa.core.get_duration(path = batch['audio_filepath'])
    except:
        print("error audio",batch['audio_filepath'])
        batch['duration'] = -1
    
    return batch
    
manifest_path = sys.argv[2]        
model_id = sys.argv[3]

language = sys.argv[4] 
language = language.capitalize()
lang_code = lang_codes[language] 

batch_size = int(sys.argv[5])
output_path = sys.argv[6] 
    
# Get local gpu rank from torch.distributed/deepspeed launcher
local_rank = int(os.getenv('LOCAL_RANK', '0'))
world_size = int(os.getenv('WORLD_SIZE', '1'))

print(
"***************** Creating model in RANK ({0}) with WORLD_SIZE = {1} *****************"
.format(local_rank,
        world_size))

config = AutoConfig.from_pretrained(
    model_id
)

model = AutoModelForSpeechSeq2Seq.from_pretrained(
        model_id
    )

processor = AutoProcessor.from_pretrained(
        model_id
    )

model = deepspeed.init_inference(model,
                                mp_size=world_size,
                                dtype=torch.float32,
                                #injection_policy={Wav2Vec2EncoderLayer: ('attention.out_proj','feed_forward.output_dense')},
                                replace_with_kernel_inject=False)
model.to(f'cuda:{local_rank}')

dataset = load_dataset('json', data_files = manifest_path)['train']

dataset = dataset.rename_column('audio_filepath', 'audio')
filepaths = dataset['audio']

dataset = dataset.add_column(name="audio_filepath", column=filepaths)
dataset = dataset.cast_column("audio", Audio())

dataset = dataset.map(get_duration, num_proc = 32)
dataset = dataset.filter(lambda sample: [samp>0 for samp in sample['duration']], batched = True, batch_size = 1000)

open(output_path, 'w').close()

st = time.time()
dataset.map(map_to_pred, batched=True, batch_size=batch_size, remove_columns=["audio"])
et = time.time()

print("time taken",(et-st)*1.0/3600)
