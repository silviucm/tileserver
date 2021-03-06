from collections import namedtuple
from cStringIO import StringIO
from ModestMaps.Core import Coordinate
from multiprocessing.pool import ThreadPool
from tilequeue.command import parse_layer_data
from tilequeue.format import extension_to_format
from tilequeue.format import json_format
from tilequeue.process import process_coord
from tilequeue.query import DataFetcher
from tilequeue.tile import coord_to_mercator_bounds
from tilequeue.tile import serialize_coord
from tilequeue.transform import mercator_point_to_wgs84
from tilequeue.utils import format_stacktrace_one_line
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response
import json
import psycopg2
import random
import shapely.geometry
import shapely.wkb
import yaml


def coord_is_valid(coord):
    if coord.zoom < 0 or coord.column < 0 or coord.row < 0:
        return False
    maxval = 2 ** coord.zoom
    if coord.column >= maxval or coord.row >= maxval:
        return False
    return True


RequestData = namedtuple('RequestData', 'layer_spec coord format')


def parse_request_path(path):
    """given a path, parse the underlying layer, coordinate, and format"""
    parts = path.split('/')
    if len(parts) != 5:
        return None
    _, layer_spec, zoom_str, column_str, row_and_ext = parts
    row_fields = row_and_ext.split('.')
    if len(row_fields) != 2:
        return None
    row_str, ext = row_fields
    format = extension_to_format.get(ext)
    if format is None:
        return None
    try:
        zoom = int(zoom_str)
        column = int(column_str)
        row = int(row_str)
    except ValueError:
        return None
    coord = Coordinate(zoom=zoom, column=column, row=row)
    if not coord_is_valid(coord):
        return None
    request_data = RequestData(layer_spec, coord, format)
    return request_data


def parse_layer_spec(layer_spec, layer_config):
    """convert a layer spec into layer_data

    returns None is any specs in the optionally comma separated list
    are unknown layers"""
    if layer_spec == 'all':
        return layer_config.all_layers
    individual_layer_names = layer_spec.split(',')
    unique_layer_names = set()
    for layer_name in individual_layer_names:
        if layer_name == 'all':
            if 'all' not in unique_layer_names:
                for all_layer_datum in layer_config.all_layers:
                    unique_layer_names.add(all_layer_datum['name'])
        unique_layer_names.add(layer_name)
    sorted_layer_names = sorted(unique_layer_names)
    layer_data = []
    for layer_name in sorted_layer_names:
        if layer_name == 'all':
            continue
        layer_datum = layer_config.layer_data_by_name.get(layer_name)
        if layer_datum is None:
            return None
        layer_data.append(layer_datum)
    return layer_data


def decode_json_tile_for_layers(tile_data, layer_data):
    layer_names_to_keep = set(ld['name'] for ld in layer_data)
    feature_layers = []
    json_data = json.loads(tile_data)
    for layer_name, json_layer_data in json_data.items():
        if layer_name not in layer_names_to_keep:
            continue
        features = []
        json_features = json_layer_data['features']
        for json_feature in json_features:
            json_geometry = json_feature['geometry']
            shape = shapely.geometry.shape(json_geometry)
            wkb = shapely.wkb.dumps(shape)
            properties = json_feature['properties']
            fid = None
            feature = wkb, properties, fid
            features.append(feature)
        feature_layer = dict(
            name=layer_name,
            features=features,
        )
        feature_layers.append(feature_layer)
    return feature_layers


class TileServer(object):

    # whether to re-raise errors on request handling
    # we want this during development, but not during production
    propagate_errors = False

    def __init__(self, layer_config, data_fetcher, post_process_data,
                 io_pool, store, redis_cache_index, health_checker=None):
        self.layer_config = layer_config
        self.data_fetcher = data_fetcher
        self.post_process_data = post_process_data
        self.io_pool = io_pool
        self.store = store
        self.redis_cache_index = redis_cache_index
        self.health_checker = health_checker

    def __call__(self, environ, start_response):
        request = Request(environ)
        try:
            response = self.handle_request(request)
        except:
            if self.propagate_errors:
                raise
            stacktrace = format_stacktrace_one_line()
            print 'Error handling request for %s: %s' % (
                request.path, stacktrace)
            response = Response(
                'Internal Server Error', status=500, mimetype='text/plain')
        return response(environ, start_response)

    def generate_404(self):
        return Response('Not Found', status=404, mimetype='text/plain')

    def create_response(self, request, tile_data, format):
        response = Response(
            tile_data,
            mimetype=format.mimetype,
            headers=[('Access-Control-Allow-Origin', '*')])
        response.add_etag()
        response.make_conditional(request)
        return response

    def handle_request(self, request):
        if (self.health_checker and
                self.health_checker.is_health_check(request)):
            return self.health_checker(request)
        request_data = parse_request_path(request.path)
        if request_data is None:
            return self.generate_404()
        layer_spec = request_data.layer_spec
        layer_data = parse_layer_spec(request_data.layer_spec,
                                      self.layer_config)
        if layer_data is None:
            return self.generate_404()

        coord = request_data.coord
        format = request_data.format

        if self.store and layer_spec != 'all' and coord.zoom <= 20:
            # we have a dynamic layer request
            # in this case, we should try to fetch the data from the
            # cache, and if present, prune the layers that aren't
            # necessary from there.
            tile_data = self.store.read_tile(coord, json_format)
            if tile_data is not None:
                # we were able to fetch the cached data
                # we'll need to decode it into the expected
                # feature_layers shape, prune the layers that aren't
                # needed, and then format the data
                feature_layers = decode_json_tile_for_layers(
                    tile_data, layer_data)
                bounds_merc = coord_to_mercator_bounds(coord)
                bounds_wgs84 = (
                    mercator_point_to_wgs84(bounds_merc[:2]) +
                    mercator_point_to_wgs84(bounds_merc[2:4]))
                tile_data_file = StringIO()
                format.format_tile(tile_data_file, feature_layers, coord,
                                   bounds_merc, bounds_wgs84)
                tile_data = tile_data_file.getvalue()
                response = self.create_response(request, tile_data, format)
                return response

        feature_data = self.data_fetcher(coord, layer_data)
        formatted_tiles = process_coord(
            coord,
            feature_data['feature_layers'],
            self.post_process_data,
            [format],
            feature_data['unpadded_bounds'],
            feature_data['padded_bounds'],
            [])
        assert len(formatted_tiles) == 1, \
            'unexpected number of tiles: %d' % len(formatted_tiles)
        formatted_tile = formatted_tiles[0]
        tile_data = formatted_tile['tile']

        # we only want to store requests for the all layer
        if self.store and layer_spec == 'all' and coord.zoom <= 20:
            self.io_pool.apply_async(
                async_store, (self.store, tile_data, coord, format))

        # update the tiles of interest set with the new coordinate
        if self.redis_cache_index:
            self.io_pool.apply_async(async_update_tiles_of_interest,
                                     (self.redis_cache_index, coord))

        response = self.create_response(request, tile_data, format)
        return response


def async_store(store, tile_data, coord, format):
    """update cache store with tile_data"""
    try:
        store.write_tile(tile_data, coord, format)
    except:
        stacktrace = format_stacktrace_one_line()
        print 'Error storing coord %s with format %s: %s' % (
            serialize_coord(coord), format.extension, stacktrace)


def async_update_tiles_of_interest(redis_cache_index, coord):
    """update tiles of interest set

    The tiles of interest represent all tiles that will get processed
    on osm diffs. Our policy is to cache tiles up to zoom level 20. As
    an optimization, because the queries only change up until zoom
    level 18, ie they are the same for z18+, we enqueue work at z18,
    and the z19 and z20 tiles get generated by cutting the z18 tile
    appropriately. This means that when we receive requests for tiles
    > z18, we need to also track the corresponding tile at z18,
    otherwise those tiles would never get regenerated.
    """
    try:
        if coord.zoom <= 20:
            redis_cache_index.index_coord(coord)
        if coord.zoom > 18:
            coord_at_z18 = coord.zoomTo(18).container()
            redis_cache_index.index_coord(coord_at_z18)
    except:
        stacktrace = format_stacktrace_one_line()
        print 'Error updating tiles of interest for coord %s: %s\n' % (
            serialize_coord(coord), stacktrace)


class LayerConfig(object):

    def __init__(self, all_layer_names, layer_data):
        self.all_layer_names = sorted(all_layer_names)
        self.layer_data = layer_data
        self.layer_data_by_name = dict(
            (layer_datum['name'], layer_datum) for layer_datum in layer_data)
        self.all_layers = [self.layer_data_by_name[x]
                           for x in self.all_layer_names]


def make_store(store_type, store_name, store_config):
    if store_type == 'directory':
        from tilequeue.store import make_tile_file_store
        return make_tile_file_store(store_name)

    elif store_type == 's3':
        from tilequeue.store import make_s3_store
        path = store_config.get('path', 'osm')
        reduced_redundancy = store_config.get('reduced_redundancy', True)
        return make_s3_store(
            store_name, path=path, reduced_redundancy=reduced_redundancy)

    else:
        raise ValueError('Unrecognized store type: `{}`'.format(store_type))


class HealthChecker(object):

    def __init__(self, url, conn_info):
        self.url = url
        conn_info_dbnames = conn_info.copy()
        self.dbnames = conn_info_dbnames.pop('dbnames')
        assert len(self.dbnames) > 0
        self.conn_info_no_dbname = conn_info_dbnames

    def is_health_check(self, request):
        return request.path == self.url

    def __call__(self, request):
        dbname = random.choice(self.dbnames)
        conn_info = dict(self.conn_info_no_dbname, dbname=dbname)
        conn = psycopg2.connect(**conn_info)
        conn.set_session(readonly=True, autocommit=True)
        try:
            cursor = conn.cursor()
            cursor.execute('select 1')
            records = cursor.fetchall()
            assert len(records) == 1
            assert len(records[0]) == 1
            assert records[0][0] == 1
        finally:
            conn.close()
        return Response('OK', mimetype='text/plain')


def create_tileserver_from_config(config):
    """create a tileserve object from yaml configuration"""
    query_config = config['queries']
    queries_config_path = query_config['config']
    template_path = query_config['template-path']
    reload_templates = query_config['reload-templates']

    with open(queries_config_path) as query_cfg_fp:
        queries_config = yaml.load(query_cfg_fp)
    all_layer_data, layer_data, post_process_data = parse_layer_data(
        queries_config, template_path, reload_templates)
    all_layer_names = [x['name'] for x in all_layer_data]
    layer_config = LayerConfig(all_layer_names, layer_data)

    conn_info = config['postgresql']
    n_conn = len(layer_data)
    io_pool = ThreadPool(n_conn)
    data_fetcher = DataFetcher(
        conn_info, all_layer_data, io_pool, n_conn)

    store = None
    store_config = config.get('store')
    if store_config:
        store_type = store_config.get('type')
        store_name = store_config.get('name')
        if store_type and store_name:
            store = make_store(store_type, store_name, store_config)

    redis_cache_index = None
    redis_config = config.get('redis')
    if redis_config:
        from redis import StrictRedis
        from tilequeue.cache import RedisCacheIndex
        redis_host = redis_config.get('host', 'localhost')
        redis_port = redis_config.get('port', 6379)
        redis_db = redis_config.get('db', 0)
        redis_client = StrictRedis(redis_host, redis_port, redis_db)
        redis_cache_index = RedisCacheIndex(redis_client)

    health_checker = None
    health_check_config = config.get('health')
    if health_check_config:
        health_check_url = health_check_config['url']
        health_checker = HealthChecker(health_check_url, conn_info)

    tile_server = TileServer(
        layer_config, data_fetcher, post_process_data, io_pool, store,
        redis_cache_index, health_checker)
    return tile_server


def wsgi_server(config_path):
    """create wsgi server given a config path"""
    with open(config_path) as fp:
        config = yaml.load(fp)
    tile_server = create_tileserver_from_config(config)
    return tile_server


if __name__ == '__main__':
    from werkzeug.serving import run_simple
    import sys

    if len(sys.argv) == 1:
        print 'Pass in path to config file'
        sys.exit(1)

    config_path = sys.argv[1]
    with open(config_path) as fp:
        config = yaml.load(fp)

    tile_server = create_tileserver_from_config(config)
    tile_server.propagate_errors = True

    server_config = config['server']
    run_simple(server_config['host'], server_config['port'], tile_server, threaded=server_config.get('threaded', False),
               use_debugger=server_config.get('debug', False),
               use_reloader=server_config.get('reload', False))
