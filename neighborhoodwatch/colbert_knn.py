import argparse
import itertools
from datetime import datetime

import requests
import datasets
import tarfile
import csv
import cudf
import cupy as cp
from tqdm import tqdm
import pyarrow as pa
import pandas as pd
import pyarrow.parquet as pq
import os
import sys
import time
import gc
import math
import numpy as np
from pylibraft.neighbors.brute_force import knn
import rmm
import logging

from pathlib import Path
from torch import Tensor

from neighborhoodwatch.cu_knn import stream_cudf_to_parquet, cleanup, tune_memory, prep_table, process_batches
from neighborhoodwatch.generate_dataset import EmbeddingGenerator, split_into_sentences, get_embeddings_from_map, \
     ParquetStreamer
from neighborhoodwatch.merge import merge_indices_and_distances
from neighborhoodwatch.neighborhoodwatch import KeepLineBreaksFormatter
from neighborhoodwatch.nw_utils import *
from rich import print as rprint
from rich.markdown import Markdown
from colbert.infra.config import ColBERTConfig
from colbert.modeling.checkpoint import Checkpoint
from colbert.indexing.collection_encoder import CollectionEncoder

from neighborhoodwatch.parquet_to_format import generate_ivec_fvec_files, generate_hdf5_file

pa.jemalloc_set_decay_ms(0)


class ColbertPreTrainedEmbeddingGenerator(EmbeddingGenerator):
    def __init__(self,
                 model_name="colbertv2.0",
                 download_pretrained_model=False,
                 chunk_size=300000,
                 normalize_embed=False):
        super().__init__(model_name, 128, chunk_size, normalize_embed)

        self.local_model_base_dir = f".pretrained"
        Path(self.local_model_base_dir).mkdir(parents=True, exist_ok=True)

        # Download the pretrained model if necessary
        self.download_colbert_model(download_pretrained_model)

        # Get Colbert collection encoder
        self.colbert_cfg = ColBERTConfig(checkpoint=f"{self.local_model_base_dir}/{self.model_name}")
        self.colbert_cp = Checkpoint(self.colbert_cfg.checkpoint, colbert_config=self.colbert_cfg)
        self.encoder = CollectionEncoder(self.colbert_cfg, self.colbert_cp)

    def download_colbert_model(self, force_download):
        local_model_file = f".pretrained/{self.model_name}.tar.gz"
        try:
            if not os.path.exists(local_model_file) or force_download is True:
                print(f"   download Colbert pre-trained model ...")
                session = requests.Session()

                model_url = "https://downloads.cs.stanford.edu/nlp/data/colbert/colbertv2/colbertv2.0.tar.gz"
                response = session.get(url=model_url, stream=True)
                with open(local_model_file, 'wb') as fh:
                    for chunk in response.iter_content(1024 * 1024 * 1024):
                        fh.write(chunk)

            tar = tarfile.open(local_model_file)
            tar.extractall(path=self.local_model_base_dir)
        except Exception as e:
            print("   ** failed to download the model", e)
            raise RuntimeError(e)

    def get_embedding_from_model(self, text):
        if isinstance(text, str):
            text = [text]

        embeddings = []
        tensor_embeddings, token_cnt = self.encoder.encode_passages(text)
        np_embed = Tensor.numpy(tensor_embeddings)
        embeddings.append(np_embed.flatten())

        return embeddings, token_cnt


def process_source_dataset(streamer,
                           dataset,
                           model_name,
                           input_dimensions,
                           token_count,
                           embedding_chunk_size,
                           column_to_embed,
                           normalize,
                           logger=None):

    processed_token_embedding_cnt = 0
    detected_zero_text_embedding_cnt = 0
    total_sentence_cnt = 0

    embedding_array = []
    
    if model_name == "colbertv2.0":
        generator = ColbertPreTrainedEmbeddingGenerator(chunk_size=embedding_chunk_size,
                                                        normalize_embed=normalize)
    else:
        assert False, f"Unsupported model name: {model_name}"

    # TODO: Batch this  for better perf
    for cur_row, row in enumerate(dataset, start=1):  # Using enumerate to track current row
        sentence_list = split_into_sentences(row[column_to_embed])

        #embedding_tuple_list, zero_embedding_cnt = get_embeddings_from_map([[cur_row, sentence_list]], generator)
        embeddings, counts = generator.generate_embedding(sentence_list)

        embeddings_length = len(embeddings)
        #embeddings_length = 1

        for j in range(embeddings_length):

            # split the embeddings into token embeddings
            token_embeddings = [embeddings[j][i * input_dimensions:(i + 1) * input_dimensions] for i in
                            range(len(embeddings[j]) // input_dimensions)]

            for token_embedding in token_embeddings:
                processed_token_embedding_cnt+= 1
                embedding_array.append(token_embedding)  # Directly extending the list
                if processed_token_embedding_cnt >= token_count:
                    break  # Early exit if token count is reached
            total_sentence_cnt += 1

        if processed_token_embedding_cnt >= token_count:
            break  # Early exit if token count is reached


    if embedding_array:
        if logger is not None:
            logger.info(f"[final] processed_token_embedding_cnt: {processed_token_embedding_cnt}")
        streamer.stream_to_parquet_without_src_metadata(embedding_array)
        embedding_array.clear()

    return (cur_row, total_sentence_cnt, processed_token_embedding_cnt, detected_zero_text_embedding_cnt)


def process_knn_computation(base_filename,
                            base_count,
                            query_filename,
                            query_count,
                            final_indices_filename,
                            final_distances_filename,
                            mem_tune=False,
                            initial_batch_size=1000000,
                            max_memory_threshold=0.1,
                            k=100,
                            split=False,
                            engine='porch'):
    if engine == 'torch':
        if initial_batch_size > 100000:
            #initial_batch_size = 10000
            initial_batch_size = 9000
    rmm.mr.set_current_device_resource(rmm.mr.PoolMemoryResource(rmm.mr.ManagedMemoryResource()))

    print(f"-- prepare query source table for brute-force KNN computation.")
    query_table = pq.read_table(query_filename).slice(0, query_count)
    print(f"   query_table.shape: {query_table.shape}")
    base_table = pq.read_table(base_filename).slice(0, base_count)
    print(f"   base_table.shape: {base_table.shape}")

    batch_size = initial_batch_size
    if mem_tune:
        batch_size = tune_memory(base_table, batch_size, max_memory_threshold, rmm)

    batch_count = math.ceil(len(base_table) / batch_size)
    assert (len(base_table) % batch_size == 0) or k <= (
            len(base_table) % batch_size), f"Cannot generate k of {k} with only {len(base_table)} rows and batch_size of {batch_size}."

    process_batches(final_indices_filename,
                    final_distances_filename,
                    base_table,
                    query_table,
                    batch_count,
                    batch_size,
                    k,
                    split,
                    engine=engine
                    #engine='torch'
                    #engine='raft'
                    #engine='cuvs'
                    )


def print_dataset_info(source_dataset_name,
                       token_count,
                       actual_row_cnt,
                       actual_sentence_cnt,
                       actual_token_embedding_counter,
                       detected_zero_embedding_cnt):
    print(f"=================================================")
    print(f"== '{source_dataset_name}' source dataset stats")
    print(f"== ----------------------------------------------")
    print(f"== Expected total count of source data tokens: {token_count}")
    print(f"== Total count of source data rows: {actual_row_cnt}")
    print(f"== Total count of sentences: {actual_sentence_cnt}")
    print(f"== Total count of token-embeddings: {actual_token_embedding_counter}")
    print(f"== Total count of detected zero sentence-embeddings: {detected_zero_embedding_cnt}")
    print(f"=================================================")


def main():
    start_time = time.time()

    parser = argparse.ArgumentParser(
        description="""ck (Colbert KNN) uses GPU acceleration to generate ground truth KNN datasets with Colbert embeddings.""",
        epilog="""
Some example commands:\n
    ck 100000 1000000 -k 100 --disable-memory-tuning
        """, formatter_class=KeepLineBreaksFormatter)
    parser.add_argument('query_token_count', type=int, help="number of query token vectors to generate")
    parser.add_argument('base_token_count', type=int, help="number of base token vectors to generate")
    parser.add_argument('-m', '--model_name', type=str, default="colbertv2.0",
                        help='Colbert model name (default: colbertv2.0)')
    parser.add_argument('-k', '--k', type=int, default=100, help='number of neighbors to compute per query vector')
    parser.add_argument('-es', '--embedding-scale', type=str, default="medium",
                        help='Embedding scale. Options: small (10000), medium(100000), large (1000000) (default: medium)')
    parser.add_argument('--data-dir', type=str, default='knn_dataset',
                        help='Directory to store the generated data (default: knn_dataset)')
    parser.add_argument('--norm-embed', action=argparse.BooleanOptionalAction, default=False,
                        help='Normalize the returned model embeddings (default: False)')
    parser.add_argument('--use-dataset-api', action=argparse.BooleanOptionalAction, default=False,
                        help='Use \'pyarrow.dataset\' API to read the dataset (default: True). Recommended for large datasets.')
    parser.add_argument('--gen-hdf5', action=argparse.BooleanOptionalAction, default=True,
                        help='Generate hdf5 files (default: True)')
    parser.add_argument('--enable-memory-tuning', action='store_true', help='Enable memory tuning')
    parser.add_argument('--disable-memory-tuning', action='store_false',
                        help='Disable memory tuning (useful for very small datasets)')
    parser.add_argument('--engine', type=str, default='torch',
                        help='Pick raft, cuvs, or torch as the brute force knn engine')

    args = parser.parse_args()

    if not check_dataset_exists_remote():
        rprint(Markdown(f"The specified wikipedia dataset configuration/subset does not exist: {BASE_CONFIG}"))
        sys.exit(1)

    rprint('', Markdown(f"""**Colbert KNN** is generating brute force neighbors based on the wikipedia dataset with the following specification:\n
--- dataset/model specification ---\n
* source dataset version: `{BASE_DATASET}-{BASE_CONFIG}`\n
* query token vector count: `{args.query_token_count}`\n
* base token vector count: `{args.base_token_count}`\n
* model name: `{args.model_name}`\n
* K: `{args.k}`\n
* embedding scale: `{args.embedding_scale}`\n
* normalize embeddings: `{args.norm_embed}`\n
--- behavior specification ---\n
* use dataset API: `{args.use_dataset_api}`\n
* generated hdf5 file: `{args.gen_hdf5}`\n
* enable memory tuning: `{args.enable_memory_tuning}`
* engine: `{args.engine}`
"""))

    model_prefix = get_model_prefix(args.model_name)
    data_dir = f"{args.data_dir}/{model_prefix}/qt{args.query_token_count}_bt{args.base_token_count}_k{args.k}"
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)

    if args.embedding_scale is None or args.embedding_scale == "medium":
        embedding_chunk_size = 100000
    elif args.embedding_scale == "small":
        embedding_chunk_size = 10000
    elif args.embedding_scale == "large":
        embedding_chunk_size = 1000000
    else:
        rprint(Markdown(f"Invalid embedding scale: {args.embedding_scale}"))
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(filename)s:%(lineno)s - %(funcName)20s() - %(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(f"{data_dir}/colbert_knn_{datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.log", mode="w"),
            logging.StreamHandler()
        ]
    )
    logger = logging.getLogger(__name__)

    input_dimensions = get_embedding_size(args.model_name)

    rprint('', Markdown("---"))
    rprint(Markdown("**Generating query dataset with embeddings ......** "), '')
    section_time = time.time()
    src_query_dataset = datasets.load_dataset(QUERY_DATASET, cache_dir=".cache", trust_remote_code=True)["train"]

    token_embed_columns = [f'token_embedding_{i}' for i in range(input_dimensions)]

    if not args.norm_embed:
        query_embed_token_filename = get_full_filename(data_dir,
                                                       f"{model_prefix}_{input_dimensions}_query_token{args.query_token_count}_src.parquet")
    else:
        query_embed_token_filename = get_full_filename(data_dir,
                                                       f"{model_prefix}_{input_dimensions}_query_token{args.query_token_count}_src_normalized.parquet")
    query_embed_streamer = ParquetStreamer(query_embed_token_filename, token_embed_columns)
    query_print_info = True
    if not os.path.exists(query_embed_token_filename):
        (query_row_cnt, query_sentence_cnt, query_embed_cnt_token,
         query_detected0cnt) = (
            process_source_dataset(query_embed_streamer,
                                   src_query_dataset,
                                   args.model_name,
                                   input_dimensions,
                                   args.query_token_count,
                                   embedding_chunk_size,
                                   "question",
                                   args.norm_embed,
                                   logger=logger))
    else:
        query_print_info = False
        print(f"The source query embed file already exists, skip processing the query source dataset.\n")

    query_embed_streamer.close()
    rprint(Markdown(
        f"(**Duration**: `{time.time() - section_time:.2f} seconds out of {time.time() - start_time:.2f} seconds`)"))

    rprint(Markdown("---"), '')
    rprint(Markdown("**Generating base dataset with embeddings ......** "), '')
    section_time = time.time()
    src_base_dataset = datasets.load_dataset(BASE_DATASET,
                                             BASE_CONFIG,
                                             cache_dir=".cache",
                                             trust_remote_code=True,
                                             split='train')

    if not args.norm_embed:
        base_embed_token_filename = get_full_filename(data_dir,
                                                      f"{model_prefix}_{input_dimensions}_base_token{args.base_token_count}_src.parquet")
    else:
        base_embed_token_filename = get_full_filename(data_dir,
                                                      f"{model_prefix}_{input_dimensions}_base_token{args.base_token_count}_src_normalized.parquet")
    base_embed_streamer = ParquetStreamer(base_embed_token_filename, token_embed_columns)
    base_print_info = True
    if not os.path.exists(base_embed_token_filename):
        (base_row_cnt, base_sentence_cnt, base_embed_cnt_token,
         base_detected0cnt) = (
            process_source_dataset(base_embed_streamer,
                                   src_base_dataset,
                                   args.model_name,
                                   input_dimensions,
                                   args.base_token_count,
                                   embedding_chunk_size,
                                   "text",
                                   args.norm_embed,
                                   logger=logger))
    else:
        base_print_info = False
        print(f"The source base embed file already exists, skip processing the base source dataset.\n")

    base_embed_streamer.close()
    rprint(Markdown(
        f"(**Duration**: `{time.time() - section_time:.2f} seconds out of {time.time() - start_time:.2f} seconds`)"))

    if query_print_info:
        print("\n")
        print_dataset_info("query",
                           args.query_token_count,
                           query_row_cnt,
                           query_sentence_cnt,
                           query_embed_cnt_token,
                           query_detected0cnt)

    if base_print_info:
        print("\n")
        print_dataset_info("base",
                           args.base_token_count,
                           base_row_cnt,
                           base_sentence_cnt,
                           base_embed_cnt_token,
                           base_detected0cnt)
    print("\n")

    rprint(Markdown("---"), '')
    rprint(Markdown("**Computing knn ......** "), '')

    if not args.norm_embed:
        final_indecies_filename = get_full_filename(data_dir,
                                                    f"{model_prefix}_{input_dimensions}_final_indices_query_token{args.query_token_count}_k{args.k}.parquet")
        final_distances_filename = get_full_filename(data_dir,
                                                     f"{model_prefix}_{input_dimensions}_final_distances_query_token{args.query_token_count}_k{args.k}.parquet")
    else:
        final_indecies_filename = get_full_filename(data_dir,
                                                    f"{model_prefix}_{input_dimensions}_final_indices_query_token{args.query_token_count}_k{args.k}_normalized.parquet")
        final_distances_filename = get_full_filename(data_dir,
                                                     f"{model_prefix}_{input_dimensions}_final_distances_query_token{args.query_token_count}_k{args.k}_normalized.parquet")

    section_time = time.time()
    process_knn_computation(base_embed_token_filename,
                            args.base_token_count,
                            query_embed_token_filename,
                            args.query_token_count,
                            final_indecies_filename,
                            final_distances_filename,
                            mem_tune=args.enable_memory_tuning,
                            k=args.k,
                            engine=args.engine)
    rprint(Markdown(
        f"(**Duration**: `{time.time() - section_time:.2f} seconds out of {time.time() - start_time:.2f} seconds`)"))

    rprint(Markdown("---"), '')
    rprint(Markdown("**Merging indices and distances ......** "), '')
    section_time = time.time()
    merge_indices_and_distances(data_dir,
                                model_prefix,
                                input_dimensions,
                                final_indecies_filename,
                                final_distances_filename,
                                args.k)
    rprint(Markdown(
        f"(**Duration**: `{time.time() - section_time:.2f} seconds out of {time.time() - start_time:.2f} seconds`)"))

    rprint(Markdown("---"), '')
    rprint(Markdown("**Generating ivec's and fvec's ......** "), '')
    section_time = time.time()
    query_vector_fvec, query_df_hdf5, base_vector_fvec, base_df_hdf5, indices_ivec, distances_fvec = \
        generate_ivec_fvec_files(data_dir,
                                 args.model_name,
                                 input_dimensions,
                                 base_embed_token_filename,
                                 query_embed_token_filename,
                                 final_indecies_filename,
                                 final_distances_filename,
                                 args.base_token_count,
                                 args.query_token_count,
                                 args.k,
                                 args.norm_embed,
                                 token_embed_columns)
    rprint(Markdown(
        f"(**Duration**: `{time.time() - section_time:.2f} seconds out of {time.time() - start_time:.2f} seconds`)"))

    rprint(Markdown("---"), '')
    if args.gen_hdf5:
        rprint(Markdown("**Generating hdf5 ......** "), '')
        section_time = time.time()
        generate_hdf5_file(data_dir,
                           model_prefix,
                           input_dimensions,
                           base_df_hdf5,
                           query_df_hdf5,
                           final_indecies_filename,
                           final_distances_filename,
                           args.base_token_count,
                           args.query_token_count,
                           args.k,
                           args.norm_embed)
        rprint(Markdown(
            f"(**Duration**: `{time.time() - section_time:.2f} seconds out of {time.time() - start_time:.2f} seconds`)"))


if __name__ == "__main__": 
    main()