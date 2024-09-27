import json
import time
import uuid
from asyncio import sleep
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

import aiohttp
import os
from uuid import uuid4
import asyncio
from aiohttp import web, ClientSession, ClientTimeout

from aiohttp.web_ws import WebSocketResponse

from bpmn_model import BpmnModel, UserFormMessage, ReceiveMessage
import aiohttp_cors
import db_connector
from functools import reduce

# Setup database
db_connector.setup_db()
routes = web.RouteTableDef()

# uuid4 = lambda: 2  # hardcoded for easy testing

models = {}

ignored_files = ["scraper2.bpmn", "fipu_ticketing.bpmn"]


def create_models():
    global models
    for file in os.listdir("models"):
        if file.endswith(".bpmn") and file not in ignored_files:
            try:
                m = BpmnModel(file)
                models[file] = m
            except Exception as e:
                print("Failed creating BPMN model from " + str(file))
                raise e

    return models


async def get_model_for_instance(iid):
    instance_dict = db_connector.get_instance(id=iid)

    # creates a new empty instance or gets an existing one
    instance = await models[instance_dict["model_path"]].create_or_get_instance(
        iid, {}
    )

    instance = await instance.run_from_log(instance_dict["events"],instance_dict["state"])

    return instance


async def run_as_server(app):
    app["bpmn_models"] = create_models()
    # db_connector.DB.drop_all_tables(with_all_data=True)
    start_time = time.time()
    logs = db_connector.get_instances_log(state="running")
    print("Running: " + str(len(logs)))
    completed = db_connector.get_instances_log(state=None)
    print("Completed: " + str(len(completed)))
    print("--- %s seconds ---" % (time.time() - start_time))

    # return
    for l in logs:
        for key, data in l.items():
            if data["model_path"] in app["bpmn_models"]:
                instance = await app["bpmn_models"][data["model_path"]].create_instance(
                    key, {}
                )
                instance = await instance.run_from_log(data["events"],data["state"])
                asyncio.create_task(instance.run())


# Placeholder for a service task
@routes.get("/test")
async def test_call(request):
    print("Called test")
    import sys
    return web.json_response({"status": "ok", "size": sys.getsizeof(models)})


@routes.get("/model")
async def get_models(request):
    data = [m.to_json() for m in models.values()]
    return web.json_response({"status": "ok", "results": data})


@routes.get("/model/{model_name}")
async def get_model(request):
    model_name = request.match_info.get("model_name")
    return web.FileResponse(
        path=os.path.join("models", app["bpmn_models"][model_name].model_path)
    )


@routes.post("/model/{model_name}/instance")
async def handle_new_instance(request):
    _id = str(uuid4())
    model = request.match_info.get("model_name")
    instance = await app["bpmn_models"][model].create_instance(_id, {})
    asyncio.create_task(instance.run())
    return web.json_response({"id": _id})


@routes.get("/model/{model_name}/instance")
async def handle_new_instance(request):
    _id = str(uuid4()) + str(uuid.uuid1())

    model = request.match_info.get("model_name")
    instance = await app["bpmn_models"][model].create_instance(_id, {})
    asyncio.create_task(instance.run())
    return web.json_response({"id": _id})


@routes.post("/instance/{instance_id}/task/{task_id}/form")
async def handle_form(request):
    post = await request.json()
    instance_id = request.match_info.get("instance_id")
    task_id = request.match_info.get("task_id")
    m = await get_model_for_instance(instance_id)
    m.instances[instance_id].in_queue.put_nowait(UserFormMessage(task_id, post))
    return web.json_response({"status": "OK"})


@routes.post("/instance/{instance_id}/task/{task_id}/receive")
async def handle_receive_task(request):
    data = await request.json()
    instance_id = request.match_info.get("instance_id")
    task_id = request.match_info.get("task_id")
    m = await get_model_for_instance(instance_id)
    m.instances[instance_id].in_queue.put_nowait(ReceiveMessage(task_id, data))
    return web.json_response({"status": "OK"})


@routes.post("/model/{model}/task/{task_id}/receive")
async def handle_auto_receive(request):
    _id = str(uuid4()) + str(uuid.uuid1())

    model = request.match_info.get("model")
    instance = await app["bpmn_models"][model].create_instance(_id, {})
    asyncio.create_task(instance.run())
    await asyncio.sleep(0.0000000000000000001)
    data = await request.json()
    instance_id = instance._id
    task_id = request.match_info.get("task_id")
    m = await get_model_for_instance(instance_id)
    m.instances[instance_id].in_queue.put_nowait(ReceiveMessage(task_id, data))
    return web.json_response({"status": "OK", "id_instance": _id})


@routes.get("/instance")
async def search_instance(request):
    params = request.rel_url.query
    queries = []
    try:
        strip_lower = lambda x: x.strip().lower()
        check_colon = lambda x: x if ":" in x else f":{x}"

        queries = list(
            tuple(
                map(
                    strip_lower,
                    check_colon(q).split(":"),
                )
            )
            for q in params["q"].split(",")
        )
    except:
        return web.json_response({"error": "invalid_query"}, status=400)

    result_ids = []
    for (att, value) in queries:
        ids = []
        for m in models.values():
            for _id, instance in m.instances.items():
                search_atts = []
                if not att:
                    search_atts = list(instance.variables.keys())
                else:
                    for key in instance.variables.keys():
                        if not att or att in key.lower():
                            search_atts.append(key)
                search_atts = filter(
                    lambda x: isinstance(instance.variables[x], str), search_atts
                )

                for search_att in search_atts:
                    if search_att and value in instance.variables[search_att].lower():
                        # data.append(instance.to_json())
                        ids.append(_id)
        result_ids.append(set(ids))

    ids = reduce(lambda a, x: a.intersection(x), result_ids[:-1], result_ids[0])

    data = []
    for _id in ids:
        data.append((await get_model_for_instance(_id)).instances[_id].to_json())

    return web.json_response({"status": "ok", "results": data})


@routes.get("/instance/{instance_id}/task/{task_id}")
async def handle_task_info(request):
    instance_id = request.match_info.get("instance_id")
    task_id = request.match_info.get("task_id")
    m: BpmnModel = get_model_for_instance(instance_id)
    if not m:
        raise aiohttp.web.HTTPNotFound
    instance = m.instances[instance_id]
    task = instance.model.elements[task_id]

    return web.json_response(task.get_info())


@routes.get("/instance/{instance_id}")
async def handle_instance_info(request):
    instance_id = request.match_info.get("instance_id")
    m = await get_model_for_instance(instance_id)
    if not m:
        raise aiohttp.web.HTTPNotFound
    instance = m.instances[instance_id].to_json()

    return web.json_response(instance)


@routes.get("/instance/{instance_id}/statews")
async def handle_instance_state_ws(request):
    ws: WebSocketResponse = web.WebSocketResponse()

    await ws.prepare(request)
    state = "running"
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            if msg.data == 'close':
                await ws.close()
            else:
                instance_id = request.match_info.get("instance_id")

                while state != "finished" and not ws.closed:

                    m = await get_model_for_instance(instance_id)
                    if not m:
                        await ws.close()
                    instance = m.instances[instance_id].to_json()

                    await ws.send_json(({"state": instance["state"]}))
                    await asyncio.sleep(3)

        elif msg.type == aiohttp.WSMsgType.ERROR:
            print('ws connection closed with exception %s' %
                  ws.exception())

    print('websocket connection closed')

    return ws


@routes.get("/instance/{instance_id}/state")
async def handle_instance_state(request):
    instance_id = request.match_info.get("instance_id")
    m = await get_model_for_instance(instance_id)
    if not m:
        raise aiohttp.web.HTTPNotFound
    instance = m.instances[instance_id].to_json()

    return web.json_response({"state": instance["state"]})


app = None


async def run():
    global app
    app = web.Application()
    app.on_startup.append(run_as_server)
    app.add_routes(routes)

    cors = aiohttp_cors.setup(
        app,
        defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
                allow_methods="*",
            )
        },
    )

    for route in list(app.router.routes()):
        cors.add(route)

    return app


async def serve():
    return run()


if __name__ == "__main__":
    app = run()
    web.run_app(app, port=os.environ.get('PORT', 9000))
