
import logging
import threading
import time
import traceback

import tornado.escape
import tornado.gen
import tornado.ioloop
import tornado.web

import errorcodes
import playhouse


CONFIG = "config.json"


def return_json(func):
    def new_func(self, *args, **kwargs):
        self.set_header("Content-Type", "application/json")
        data = func(self, *args, **kwargs)
        logging.debug("Sent response %s", data)
        self.write(tornado.escape.json_encode(data))
    return new_func

def json_parser(func):
    def new_post(self, *args, **kwargs):
        try:
            data = tornado.escape.json_decode(self.request.body)
            return func(self, data, *args, **kwargs)
        except UnicodeDecodeError:
            return errorcodes.NOT_UNICODE
        except ValueError:
            return errorcodes.INVALID_JSON
    return new_post

def json_validator(jformat):
    def decorator(func):
        def is_valid(data, jf):
            logging.debug("Testing %s vs %s", repr(data), jf)
            if type(jf) is dict:
                # handle optional keys (?-prefixed)
                all_keys = set(x[1:] if x[0] == '?' else x for x in jf)
                required_keys = set(x for x in jf if x[0] != '?')
                jf = {k[1:] if k[0] == '?' else k: v for k, v in jf.items()}
            # don't even ask
            return (type(jf) is list and type(data) is list and all(is_valid(d, jf[0]) for d in data)) or \
                   (type(jf) is tuple and type(data) is list and len(data) == len(jf) and all(is_valid(a, b) for a, b in zip(data, jf))) or \
                   (type(jf) is dict and type(data) is dict and data.keys() <= all_keys and data.keys() >= required_keys and all(is_valid(data[k], jf[k]) for k in data)) or \
                   (type(jf) is set and type(data) in jf) or \
                   (type(jf) is type and type(data) is jf)
        
        def new_func(self, data, *args, **kwargs):
            logging.debug("Got request %s", data)
            if is_valid(data, jformat):
                return func(self, data, *args, **kwargs)
            else:
                logging.debug("Request was invalid")
                return errorcodes.INVALID_FORMAT
        
        return new_func
    
    return decorator


class LightsHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator([{"x": int, "y": int, "change": dict}])
    def post(self, data):
        for light in data:
            try:
                grid.set_state(light['x'], light['y'], **light['change'])
            except playhouse.NoBridgeAtCoordinateException:
                logging.warning("No bridge added for (%s,%s)", light['x'], light['y'])
                logging.debug("", exc_info=True)
            except playhouse.OutsideGridException:
                logging.warning("(%s,%s) is outside grid bounds", light['x'], light['y'])
                logging.debug("", exc_info=True)
        grid.commit()
        return {"state": "success"}

class LightsAllHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator(dict)
    def post(self, data):
        grid.set_all(**data)
        grid.commit()
        return {"state": "success"}

class BridgesHandler(tornado.web.RequestHandler):
    @return_json
    def get(self):
        res = {
            "bridges": {
                mac: {
                    "ip": bridge.ipaddress,
                    "username": bridge.username,
                    "valid_username": bridge.logged_in,
                    "lights": len(bridge.get_lights()) if bridge.logged_in else -1
                }
                for mac, bridge in grid.bridges.items()
            },
            "state": "success"
        }
        return res

class BridgesAddHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator({"ip": str, "?username": {str, type(None)}})
    def post(self, data):
        try:
            username = data.get("username", None)
            bridge = grid.add_bridge(data['ip'], username)
        except playhouse.BridgeAlreadyAddedException:
            return errorcodes.BRIDGE_ALREADY_ADDED
        except:
            return errorcodes.BRIDGE_NOT_FOUND.format(ip=data['ip'])
        return {"state": "success",
                "bridges": {
                    bridge.serial_number: {
                        "ip": bridge.ipaddress,
                        "username": bridge.username,
                        "valid_username": bridge.logged_in,
                        "lights": len(bridge.get_lights()) if bridge.logged_in else -1
                    }}}

class BridgesMacHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator({"username": {str, type(None)}})
    def post(self, data, mac):
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        grid.bridges[mac].set_username(data['username'])
        return {"state": "success", "username": data['username'], "valid_username": grid.bridges[mac].logged_in}

    @return_json
    def delete(self, mac):
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        
        del grid.bridges[mac]
        return {"state": "success"}


class BridgeLightsHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator([{"light": int, "change": dict}])
    def post(self, data, mac):
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        
        for light in data:
            grid.bridges[mac].set_state(light['light'], **light['change'])
        
        return {'state': 'success'}


class BridgeLightsAllHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator(dict)
    def post(self, data, mac):
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        
        grid.bridges[mac].set_group(0, **data)
        
        return {'state': 'success'}


class BridgeLampSearchHandler(tornado.web.RequestHandler):
    @return_json
    def post(self, mac):        
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        grid.bridges[mac].search_lights()
        return {"state": "success"}


class BridgeAddUserHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator({"?username": str})
    def post(self, data, mac):
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        username = data.get("username", None)
        
        try:
            newname = grid.bridges[mac].create_user("playhouse user", username)
            return {"state": "success", "username": newname}
        except playhouse.NoLinkButtonPressedException:
            return errorcodes.NO_LINKBUTTON
        except Exception:
            logging.debug("", exc_info=True)
            return errorcodes.INVALID_NAME


event = threading.Event()
# later changes to the bridges if auto_add is True will be reflected
# in new_bridges
new_bridges = []
last_search = -1

class BridgesSearchHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator({"auto_add":bool})
    def post(self, data):
        if event.is_set():
            return errorcodes.CURRENTLY_SEARCHING
        
        def myfunc():
            global new_bridges, last_search
            nonlocal data
            event.set()
            
            logging.info("Running bridge discovery")
            new_bridges = playhouse.discover()
            logging.debug("Bridges found: %s", new_bridges)
            
            last_search = int(time.time())
            if data['auto_add']:
                logging.info("Auto-adding bridges")
                for b in new_bridges:
                    try:
                        grid.add_bridge(b)
                        logging.info("Added %s", b.serial_number)
                    except playhouse.BridgeAlreadyAddedException:
                        logging.info("%s already added", b.serial_number)
            logging.info("Bridge discovery finished")
            event.clear()
        thread = threading.Thread()
        thread.run = myfunc
        thread.start()
        
        return {"state": "success"}
    
    @return_json
    def get(self):
        if event.is_set():
            return errorcodes.CURRENTLY_SEARCHING
        
        return {
            "state": "success",
            "finished": last_search,
            "bridges": {
                b.serial_number: {
                    "ip": b.ipaddress,
                    "username": b.username,
                    "valid_username": b.logged_in,
                    "lights": len(b.get_lights()) if b.logged_in else -1
                }
                for b in new_bridges
            }
        }


class GridHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator([[{"mac": str, "lamp": int}]])
    def post(self, data):
        g = [[(lamp['mac'], lamp['lamp']) for lamp in row] for row in data]
        grid.set_grid(g)
        logging.debug("Grid is set to %s", g)
        return {"state": "success"}
            
    @return_json
    def get(self):
        data = [[{"mac": mac, "lamp": lamp} for mac, lamp in row] for row in grid.grid]
        return {"state":"success", "grid":data, "width":grid.width, "height":grid.height}

class BridgesSaveHandler(tornado.web.RequestHandler):
    @return_json
    def post(self):
        with open(CONFIG, 'r+') as f:
            conf = tornado.escape.json_decode(f.read())
            conf['ips'] = [bridge.ipaddress for bridge in grid.bridges.values()]
            conf['usernames'] = {bridge.serial_number: bridge.username for bridge in grid.bridges.values()}
            f.seek(0)
            f.write(tornado.escape.json_encode(conf))
            f.truncate()
        return {"state": "success"}

class GridSaveHandler(tornado.web.RequestHandler):
    @return_json
    def post(self):
        with open(CONFIG, 'r+') as f:
            conf = tornado.escape.json_decode(f.read())
            conf['grid'] = grid.grid
            f.seek(0)
            f.write(tornado.escope.json_encode(conf))
            f.truncate()
        return {"state": "success"}
        
class DebugHandler(tornado.web.RequestHandler):
    def get(self):
        website = """
<!DOCTYPE html>
<html>
<head><title>Debug</title></head>
<script>
function send_get(){
    var req = new XMLHttpRequest();
    url = document.getElementById('url').value;
    req.open("GET",url,false);
    req.send(null);
    response = req.responseText;
    document.getElementById('response').value = response;
}

function send_post(){
    var req = new XMLHttpRequest();
    url = document.getElementById('url').value;
    request = document.getElementById('request').value;
    req.open("POST",url,false);
    req.setRequestHeader("Content-type", "application/json");
    req.setRequestHeader("Content-length", request.length);
    req.setRequestHeader("Connection", "close");
    req.send(request);
    response = req.responseText;
    document.getElementById('response').value = response;
}
</script>
<body>

<h2>Request</h2>
<button type="button" onclick="send_get()">GET</button>
<button type="button" onclick="send_post()">POST</button><br />
<input type="text" name="url" id="url"><br />
<textarea rows="10" cols="50" id="request"></textarea>
<h2>Response</h2>
<textarea readonly="readonly" rows="10" cols="50" id="response"></textarea>
 
</body>
</html>



</html>

        
        """
        self.write(website)

class StatusHandler(tornado.web.RequestHandler):
    def get(self):
        pass

application = tornado.web.Application([
    (r'/lights', LightsHandler),
    (r'/lights/all', LightsAllHandler),
    (r'/bridges', BridgesHandler),
    (r'/bridges/add', BridgesAddHandler),
    (r'/bridges/([0-9a-f]{12})', BridgesMacHandler),
    (r'/bridges/([0-9a-f]{12})/lampsearch', BridgeLampSearchHandler),
    (r'/bridges/([0-9a-f]{12})/adduser', BridgeAddUserHandler),
    (r'/bridges/([0-9a-f]{12})/lights', BridgeLightsHandler),
    (r'/bridges/([0-9a-f]{12})/lights/all', BridgeLightsAllHandler),
    (r'/bridges/search', BridgesSearchHandler),
    (r'/grid', GridHandler),
    (r'/bridges/save', BridgesSaveHandler),
    (r'/grid/save', GridSaveHandler),
    (r'/debug', DebugHandler),
    (r'/status', StatusHandler),
])


def init_lightgrid():
    
    logging.info("Reading configuration file")
    
    with open(CONFIG, 'r') as file:
        config = tornado.escape.json_decode(file.read())
        logging.debug("Configuration was %s", config)
        
        config["grid"] = [ [ (x[0], x[1]) for x in row ] for row in config["grid"] ]
        logging.debug("Constructed grid %s", config["grid"])
    
    logging.debug("Instatiating LightGrid")
    grid = playhouse.LightGrid(config["usernames"], config["grid"], buffered=True)
    
    logging.info("Adding preconfigured bridges")
    for ip in config["ips"]:
        try:
            bridge = grid.add_bridge(ip)
            logging.info("Added bridge %s at %s", bridge.serial_number, bridge.ipaddress)
        except Exception as e:
            logging.warning("Couldn't find a bridge at %s", ip)
            logging.debug("", exc_info=True)
    logging.info("Finished adding bridges")
    
    return grid



if __name__ == "__main__":
    format_string = "%(created)d:%(levelname)s:%(module)s:%(funcName)s:%(lineno)d > %(message)s"
    formatter = logging.Formatter(format_string)
    
    logging.basicConfig(filename="lightserver-all.log",
                        level=logging.DEBUG,
                        format=format_string)
    
    file_handler = logging.FileHandler(filename="lightserver.log")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    
    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(formatter)
    
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().addHandler(stderr_handler)
    
    logging.info("Initializing light server")
    
    grid = init_lightgrid()

    application.listen(4711)
    
    logging.info("Server now listening at port 4711")
    tornado.ioloop.IOLoop.instance().start()