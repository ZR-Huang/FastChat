"""
A multi-model worker that contains multiple sub-works one for each model.  This
supports running a list of models on the same machine so that they can
(potentially) share the same background weights.

Each model can have one or more model names.

This multi-model worker assumes the models shares some underlying weights and
thus reports the combined queue lengths for health checks.

We recommend using this with multiple Peft models (with `peft` in the name)
where all Peft models are trained on the exact same base model.
"""
import argparse
import asyncio
import dataclasses
import logging
import json
import os
import time
from typing import List, Union
import threading
import uuid

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
import requests

from fastchat.modules.gptq import GptqConfig

try:
    from transformers import (
        AutoTokenizer,
        AutoModelForCausalLM,
        LlamaTokenizer,
        AutoModel,
    )
except ImportError:
    from transformers import (
        AutoTokenizer,
        AutoModelForCausalLM,
        LLaMATokenizer,
        AutoModel,
    )
import torch
import torch.nn.functional as F
import uvicorn

from fastchat.constants import WORKER_HEART_BEAT_INTERVAL, ErrorCode, SERVER_ERROR_MSG
from fastchat.model.model_adapter import (
    load_model,
    add_model_args,
    get_conversation_template,
)
from fastchat.model.model_chatglm import generate_stream_chatglm
from fastchat.model.model_falcon import generate_stream_falcon
from fastchat.model.model_codet5p import generate_stream_codet5p
from fastchat.serve.inference import generate_stream
from fastchat.serve.model_worker import ModelWorker
from fastchat.utils import build_logger, pretty_print_semaphore, get_context_length


worker_id = str(uuid.uuid4())[:6]
logger = build_logger("model_worker", f"model_worker_{worker_id}.log")

# We store both the underlying workers and a mapping from their model names to
# the worker instance.  This makes it easy to fetch the appropriate worker for
# each API call.
workers = []
worker_map = {}

# Note: For now the semaphore locks access to all models managed by the worker.
# This makes sense when all models are Peft models sharing the same underlying
# base model weights.  It probably doesn't make sense in other scenarios.
global_counter = 0
model_semaphore = None


def heart_beat_worker(controller):
    while True:
        time.sleep(WORKER_HEART_BEAT_INTERVAL)
        controller.send_heart_beat()


app = FastAPI()


def release_model_semaphore():
    model_semaphore.release()


def acquire_model_semaphore():
    global model_semaphore, global_counter
    global_counter += 1
    if model_semaphore is None:
        model_semaphore = asyncio.Semaphore(args.limit_model_concurrency)
    return model_semaphore.acquire()


def create_background_tasks():
    background_tasks = BackgroundTasks()
    background_tasks.add_task(release_model_semaphore)
    return background_tasks


# Note: for all the calls below, we make a hard assumption that the caller
# includes the model name in the payload, otherwise we can't figure out which
# underlying sub-worker to call.


@app.post("/worker_generate_stream")
async def api_generate_stream(request: Request):
    params = await request.json()
    await acquire_model_semaphore()
    worker = worker_map[params["model"]]
    generator = worker.generate_stream_gate(params)
    background_tasks = create_background_tasks()
    return StreamingResponse(generator, background=background_tasks)


@app.post("/worker_generate")
async def api_generate(request: Request):
    params = await request.json()
    print(params)
    await acquire_model_semaphore()
    worker = worker_map[params["model"]]
    output = worker.generate_gate(params)
    release_model_semaphore()
    return JSONResponse(output)


@app.post("/worker_get_embeddings")
async def api_get_embeddings(request: Request):
    params = await request.json()
    await acquire_model_semaphore()
    worker = worker_map[params["model"]]
    embedding = worker.get_embeddings(params)
    background_tasks = create_background_tasks()
    return JSONResponse(content=embedding, background=background_tasks)


@app.post("/worker_get_status")
async def api_get_status(request: Request):
    return {
        "model_names": [m for w in workers for m in w.model_names],
        "speed": 1,
        "queue_length": sum([w.get_queue_length() for w in workers]),
    }


@app.post("/count_token")
async def api_count_token(request: Request):
    params = await request.json()
    worker = worker_map[params["model"]]
    return worker.count_token(params)


@app.post("/worker_get_conv_template")
async def api_get_conv(request: Request):
    params = await request.json()
    worker = worker_map[params["model"]]
    return worker.get_conv_template()


@app.post("/model_details")
async def api_model_details(request: Request):
    params = await request.json()
    print(params)
    worker = worker_map[params["model"]]
    return {"context_length": worker.context_len}


if __name__ == "__main__":
    # Note: Ensure we resolve arg conflicts.  We let `add_model_args` add MOST
    # of the model args but we'll override one to have an append action that
    # supports multiple values.
    parser = argparse.ArgumentParser(conflict_handler="resolve")
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=21002)
    parser.add_argument("--worker-address", type=str, default="http://localhost:21002")
    parser.add_argument(
        "--controller-address", type=str, default="http://localhost:21001"
    )
    add_model_args(parser)
    # Override the model path to be repeated and align it with model names.
    parser.add_argument(
        "--model-path",
        type=str,
        default=[],
        action="append",
        help="One or more paths to model weights to load. This can be a local folder or a Hugging Face repo ID.",
    )
    parser.add_argument(
        "--model-names",
        type=lambda s: s.split(","),
        action="append",
        help="One or more model names.  Values must be aligned with `--model-path` values.",
    )
    parser.add_argument("--limit-model-concurrency", type=int, default=5)
    parser.add_argument("--stream-interval", type=int, default=2)
    parser.add_argument("--no-register", action="store_true")
    args = parser.parse_args()
    logger.info(f"args: {args}")

    if args.gpus:
        if len(args.gpus.split(",")) < args.num_gpus:
            raise ValueError(
                f"Larger --num-gpus ({args.num_gpus}) than --gpus {args.gpus}!"
            )
        os.environ["CUDA_VISIBLE_DEVICES"] = args.gpus

    gptq_config = GptqConfig(
        ckpt=args.gptq_ckpt or args.model_path,
        wbits=args.gptq_wbits,
        groupsize=args.gptq_groupsize,
        act_order=args.gptq_act_order,
    )

    workers = []
    model_specs = zip(
        args.model_path, args.model_names if args.model_names else args.model_path
    )
    for model_path, model_names in model_specs:
        w = ModelWorker(
            args.controller_address,
            args.worker_address,
            worker_id,
            model_path,
            model_names,
            args.no_register,
            args.device,
            args.num_gpus,
            args.max_gpu_memory,
            args.load_8bit,
            args.cpu_offloading,
            gptq_config,
            args.stream_interval,
        )
        workers.append(w)
        for model_name in model_names:
            worker_map[model_name] = w

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
