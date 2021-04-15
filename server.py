import aiohttp
from aiohttp import web
from uuid import uuid4
import asyncio
from bpmn_model import BpmnModel, UserFormMessage
import random
import sys

uuid4 = lambda: 1


m = BpmnModel("models/model_01.bpmn")


async def run_with_server(app):
    app["bpmn_model"] = m


async def handle_new(request):
    _id = str(uuid4())
    asyncio.create_task(app["bpmn_model"].run(_id, {}))
    return web.json_response({"id": _id})


async def handle_form(request):
    post = await request.json()
    instance_id = request.match_info.get("instance_id")
    task_id = request.match_info.get("task_id")
    app["bpmn_model"].instances[instance_id].in_queue.put_nowait(
        UserFormMessage(task_id, post)
    )

    return web.json_response({"status": "OK"})


async def handle_instance_info(request):
    instance_id = request.match_info.get("instance_id")
    if instance_id not in app["bpmn_model"].instances:
        raise aiohttp.web.HTTPNotFound
    instance = app["bpmn_model"].instances[instance_id]

    return web.json_response(instance.get_info())


app = web.Application()
app.on_startup.append(run_with_server)
app.add_routes([web.post("/instance", handle_new)])
app.add_routes([web.post("/instance/{instance_id}/task/{task_id}/form", handle_form)])
app.add_routes([web.get("/instance/{instance_id}", handle_instance_info)])

web.run_app(app)