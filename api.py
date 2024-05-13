"""API module for FastAPI"""
import requests
from typing import Callable, Dict, Optional
from threading import Lock
from secrets import compare_digest
import asyncio
from collections import defaultdict
from hashlib import sha256
import string
from random import choices

from modules import shared  # pylint: disable=import-error
from modules.api.api import decode_base64_to_image  # pylint: disable=E0401
from modules.call_queue import queue_lock  # pylint: disable=import-error
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import api_models as models
from scripts.mergers import pluslora

from fastapi import File, UploadFile, Form
from typing import Annotated
import shutil
from modules.progress import create_task_id, add_task_to_queue, start_task, finish_task, current_task
from time import sleep
from datetime import datetime
import subprocess
import os
from dotenv import load_dotenv

load_dotenv()


class Api:
    """Api class for FastAPI"""

    def __init__(
        self, app: FastAPI, qlock: Lock, prefix: Optional[str] = None
    ) -> None:
        if shared.cmd_opts.api_auth:
            self.credentials = {}
            for auth in shared.cmd_opts.api_auth.split(","):
                user, password = auth.split(":")
                self.credentials[user] = password

        self.app = app
        self.queue: Dict[str, asyncio.Queue] = {}
        self.res: Dict[str, Dict[str, Dict[str, float]]] = \
            defaultdict(dict)
        self.queue_lock = qlock
        self.tasks: Dict[str, asyncio.Task] = {}

        self.runner: Optional[asyncio.Task] = None
        self.prefix = prefix
        self.running_batches: Dict[str, Dict[str, float]] = \
            defaultdict(lambda: defaultdict(int))

        # self.add_api_route(
        #     'interrogate',
        #     self.endpoint_interrogate,
        #     methods=['POST'],
        #     response_model=models.TaggerInterrogateResponse
        # )

        # self.add_api_route(
        #     'interrogators',
        #     self.endpoint_interrogators,
        #     methods=['GET'],
        #     response_model=models.TaggerInterrogatorsResponse
        # )

        self.add_api_route(
            'unload-interrogators',
            self.endpoint_unload_interrogators,
            methods=['POST'],
            response_model=str,
        )

        self.add_api_route(
            'merge-lora',
            self.merge_lora_api,
            methods=['POST'],
            response_model=models.MergeLoraResponse,
        )

        self.add_api_route(
            'upload-lora',
            self.upload_lora_api,
            methods=['POST'],
            response_model=models.UploadLoraResponse,
        )

        self.add_api_route(
            'upload-lora-merge-checkpoint',
            self.upload_lora_and_merge_lora_to_checkpoint,
            methods=['POST'],
            response_model=models.UploadLoraMergeLoraResponse,
        )

    async def add_to_queue(self, m, q, n='', i=None, t=0.0) -> Dict[
        str, Dict[str, float]
    ]:
        if m not in self.queue:
            self.queue[m] = asyncio.Queue()
        #  loop = asyncio.get_running_loop()
        #  asyncio.run_coroutine_threadsafe(
        task = asyncio.create_task(self.queue[m].put((q, n, i, t)))
        #  , loop)

        if self.runner is None:
            loop = asyncio.get_running_loop()
            asyncio.ensure_future(self.batch_process(), loop=loop)
        await task
        return await self.tasks[q+"\t"+n]

    async def do_queued_interrogation(self, m, q, n, i, t) -> Dict[
        str, Dict[str, float]
    ]:
        self.running_batches[m][q] += 1.0
        # queue and name empty to process, not queue
        res = self.endpoint_interrogate(
            models.TaggerInterrogateRequest(
                image=i,
                model=m,
                threshold=t,
                name_in_queue='',
                queue=''
            )
        )
        self.res[q][n] = res.caption["tag"]
        for k, v in res.caption["rating"].items():
            self.res[q][n]["rating:"+k] = v
        return self.running_batches

    async def finish_queue(self, m, q) -> Dict[str, Dict[str, float]]:
        if q in self.running_batches[m]:
            del self.running_batches[m][q]
        if q in self.res:
            return self.res.pop(q)
        return self.running_batches

    async def batch_process(self) -> None:
        #  loop = asyncio.get_running_loop()
        while len(self.queue) > 0:
            for m in self.queue:
                # if zero the queue might just be pending
                while True:
                    try:
                        #  q, n, i, t = asyncio.run_coroutine_threadsafe(
                        #  self.queue[m].get_nowait(), loop).result()
                        q, n, i, t = self.queue[m].get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    self.tasks[q+"\t"+n] = asyncio.create_task(
                        self.do_queued_interrogation(m, q, n, i, t) if n != ""
                        else self.finish_queue(m, q)
                    )

            for model in self.running_batches:
                if len(self.running_batches[model]) == 0:
                    del self.queue[model]
            else:
                await asyncio.sleep(0.1)

        self.running_batches.clear()
        self.runner = None

    def auth(self, creds: Optional[HTTPBasicCredentials] = None):
        if creds is None:
            creds = Depends(HTTPBasic())
        if creds.username in self.credentials:
            if compare_digest(creds.password,
                              self.credentials[creds.username]):
                return True

        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={
                "WWW-Authenticate": "Basic"
            })

    def add_api_route(self, path: str, endpoint: Callable, **kwargs):
        if self.prefix:
            path = f'{self.prefix}/{path}'

        if shared.cmd_opts.api_auth:
            return self.app.add_api_route(path, endpoint, dependencies=[
                Depends(self.auth)], **kwargs)
        return self.app.add_api_route(path, endpoint, **kwargs)

    async def queue_interrogation(self, m, q, n='', i=None, t=0.0) -> Dict[
        str, Dict[str, float]
    ]:
        """ queue an interrogation, or add to batch """
        if n == '':
            task = asyncio.create_task(self.add_to_queue(m, q))
        else:
            if n == '<sha256>':
                n = sha256(i).hexdigest()
                if n in self.res[q]:
                    return self.running_batches
            elif n in self.res[q]:
                # clobber name if it's already in the queue
                j = 0
                while f'{n}#{j}' in self.res[q]:
                    j += 1
                n = f'{n}#{j}'
            self.res[q][n] = {}
            # add image to queue
            task = asyncio.create_task(self.add_to_queue(m, q, n, i, t))
        return await task

    def endpoint_unload_interrogators(self):
        unloaded_models = 0

        return f"Successfully unload {unloaded_models} model(s)"

    def merge_lora(self, request: models.MergeLoraRequest) -> str:
        """Merge Lora"""
        try:
            # comment:

            request.loraratios = "\
                NONE:0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0\n\
                ALL:1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1\n\
                INS:1,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0\n\
                IND:1,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0\n\
                INALL:1,1,1,1,1,1,1,0,0,0,0,0,0,0,0,0,0\n\
                MIDD:1,0,0,0,1,1,1,1,1,1,1,1,0,0,0,0,0\n\
                OUTD:1,0,0,0,0,0,0,0,1,1,1,1,0,0,0,0,0\n\
                OUTS:1,0,0,0,0,0,0,0,0,0,0,0,1,1,1,1,1\n\
                OUTALL:1,0,0,0,0,0,0,0,1,1,1,1,1,1,1,1,1\n\
                ALL0.5:0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5,0.5"

            request.lnames = f"{request.lnames}"
            data = request

            res = pluslora.pluslora(
                loraratios=data.loraratios,
                calc_precision=data.calc_precision,
                device=data.device,
                lnames=data.lnames,
                metasets=data.metasets,
                model=data.model,
                output=data.output,
                save_precision=data.save_precision,
                settings=[]
            )

            return res

        except Exception as e:
            raise e
        # end try

    def referesh_loras_request(self):
        """Refresh Loras"""
        try:
            # comment:
            print("Refresh Loras")
            res = requests.post("http://127.0.0.1:7860/sdapi/v1/refresh-loras")
            print("Refresh Loras response: ", res.text)
            return res

        except Exception as e:
            raise e
        # end try

    def referesh_checkpoints_request(self):
        """Refresh Checkpoints"""
        try:
            # comment:
            print("Refresh checkpoints")
            res = requests.post("http://127.0.0.1:7860/sdapi/v1/refresh-checkpoints")
            print("Refresh checkpoints response: ", res.text)
            return res

        except Exception as e:
            raise e
        # end try

    def merge_lora_api(self, request: models.MergeLoraRequest):
        """Merge Lora"""
        try:
            # comment:

            res = self.merge_lora(request)

            return models.MergeLoraResponse(checkpoint_merged_path=res)

        except Exception as e:
            raise e
        # end try

    def upload_file(self, file: UploadFile):
        try:
            # save lora file to disk
            file_location = f"models/Lora/{file.filename}"
            with open(file_location, "wb+") as file_object:
                shutil.copyfileobj(file.file, file_object)
            message = f'{file.filename} saved at {file_location}'

            return message
        except Exception as e:
            raise e
        # end try

    def upload_lora_api(self, lora_file: UploadFile):
        """Upload Lora"""
        try:
            # comment:
            message = self.upload_file(lora_file)
            self.referesh_loras_request()

            return models.UploadLoraResponse(message=message)
        except Exception as e:
            raise e
        # end try

    def upload_lora_and_merge_lora_to_checkpoint(self, lora_file: UploadFile, merge_request: models.UploadLoraMergeLoraRequest = Depends()):
        """Upload Lora and merge Lora to checkpoint"""
        try:

            # task_id = create_task_id("txt2img")
            task_merge_normal_lora_id = create_task_id("txt2img")
            task_merge_lcm_lora_id = create_task_id("txt2img")
            task_refresh_checkpoints_id = create_task_id("txt2img")

            print("task merge normal lora id: ", task_merge_normal_lora_id)
            print("task merge lcm lora id: ", task_merge_lcm_lora_id)

            add_task_to_queue(task_merge_normal_lora_id)
            add_task_to_queue(task_merge_lcm_lora_id)
            add_task_to_queue(task_refresh_checkpoints_id)

            # comment:

            print("Merge Request:   ", merge_request)
            lora_file_name = lora_file.filename.split(".")[0]

            with self.queue_lock:

                try:
                    dt_str = datetime.now().strftime("%Y%m%d%H%M%S")
                    shared.state.begin(job="scripts_txt2img")
                    start_task(task_merge_normal_lora_id)

                    upload_res = self.upload_file(lora_file)
                    print("Uploaded file successfully:   ", upload_res)
                    self.referesh_loras_request()

                    # merge normal lora
                    print("1. Started to merge normal lora")
                    normal_lora_reques = models.UploadLoraMergeLoraRequest()
                    normal_lora_reques.lnames = f"{lora_file_name}:0.8"
                    normal_lora_reques.calc_precision = "float"
                    normal_lora_reques.save_precision = "fp16"
                    normal_lora_reques.remake_dimension = "no"
                    normal_lora_reques.output = f"checkpoint_merged_normal_lora_{lora_file_name}_{dt_str}"
                    normal_lora_reques.model = merge_request.model

                    checkpoint_merged_res = self.merge_lora(normal_lora_reques)
                    checkpoint_merged_name = checkpoint_merged_res.split(
                        "/")[-1]
                    message = f"1. Upload and merge lora <{normal_lora_reques.lnames}> to <{normal_lora_reques.model}> successfully. ==> <{checkpoint_merged_name}>"
                    print("Merged normal lora successfully:   ",
                          checkpoint_merged_res)

                    finish_task(task_merge_normal_lora_id)
                    start_task(task_refresh_checkpoints_id)
                    shared.refresh_checkpoints()
                    finish_task(task_refresh_checkpoints_id)

                    # merge lora
                    if merge_request.is_with_lcm == True:
                        print("2. Started to merge LCM lora")
                        start_task(task_merge_lcm_lora_id)
                        lcm_lora_request = models.UploadLoraMergeLoraRequest()
                        lcm_lora_request.lnames = f"pytorch_lora_weights:0.7"
                        lcm_lora_request.calc_precision = "float"
                        lcm_lora_request.save_precision = "float"
                        lcm_lora_request.remake_dimension = "auto"
                        lcm_lora_request.model = checkpoint_merged_name
                        lcm_lora_request.output = merge_request.output

                        checkpoint_merged_res = self.merge_lora(
                            lcm_lora_request)
                        checkpoint_merged_name = checkpoint_merged_res.split(
                            "/")[-1]
                        message_lcm = f"2. Merge LCM lora <{lcm_lora_request.lnames}> to <{lcm_lora_request.model}> successfully. ==> <{checkpoint_merged_name}>"
                        print("Merged LCM lora successfully:   ",
                              checkpoint_merged_res)
                        shared.refresh_checkpoints()
                        finish_task(task_merge_lcm_lora_id)
                        message = f"{message}, {message_lcm}"

                    print('Merge checkpoint response:: ', checkpoint_merged_res)
                    self.copy_checkpoint(checkpoint_merged_res)

                finally:
                    shared.state.end()
                    shared.total_tqdm.clear()

            return models.UploadLoraMergeLoraResponse(message=message, checkpoint_merged_name=checkpoint_merged_name)
        except Exception as e:
            raise e
        finally:
            print("Finish task")
        # end try

    def copy_checkpoint(self, source_file):
        print('Source file:: ', source_file)
        pem_file = os.environ['PEM_PATH']
        server_address = os.environ['SERVER_ADDRESS']
        destination_file = os.environ['DESTINATION_FILE']

        command = ["sudo", "scp", "-i", pem_file, source_file, server_address + ":" + destination_file]

        try:
            subprocess.run(command)
            print("File copied successfully!")
        except subprocess.CalledProcessError as e:
            print("Error copying file:", e.output)
            raise e

def on_app_started(_, app: FastAPI):
    Api(app, queue_lock, '/supermerger/v1')
