# backend.py
# openeventdatabase

from datetime import datetime
import json
import os
import re
import subprocess

import falcon
import psycopg2
import psycopg2.extras


def db_connect():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "oedb"),
        host=os.getenv("DB_HOST", None),
        password=os.getenv("POSTGRES_PASSWORD", None),
        user=os.getenv("DB_USER", None))


class EventEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime):
            return o.isoformat()
        try:
            return super().default(o)
        except TypeError:
            return str(o)


def dumps(data):
    return json.dumps(data, cls=EventEncoder, sort_keys=True)


class HeaderMiddleware:

    def process_response(self, req, resp, resource):
        resp.set_header('X-Powered-By', 'OpenEventDatabase')
        resp.set_header('Access-Control-Allow-Origin', '*')
        resp.set_header('Access-Control-Allow-Headers', 'X-Requested-With')
        resp.set_header('Access-Control-Allow-Headers', 'Content-Type')

class StatsResource(object):
    def on_get(self, req, resp):
        db = db_connect()
        cur = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
        # estimated row count, way faster then count(*)
        cur.execute("SELECT reltuples FROM pg_class r WHERE relname = 'events';")
        count = cur.fetchone()[0]
        cur.execute("SELECT max(lastupdate) as last_updated, current_timestamp-pg_postmaster_start_time() from events;")
        pg_stats = cur.fetchone()
        last = pg_stats[0]
        pg_uptime = pg_stats[1]
        uptime = subprocess.check_output(["uptime","-p"]).decode('utf-8')[0:-1]
        cur.close()
        db.close()
        resp.body = dumps(dict(events_count=count, last_updated=last, uptime=uptime, db_uptime=pg_uptime))
        resp.status = falcon.HTTP_200


class BaseEvent:

    def row_to_feature(self, row, geom_only = False):
        # only return geometry and event id
        if geom_only:
          return {
            "type": "Feature",
            "geometry": json.loads(row['geometry']),
            "properties": { "id" : row['events_id'] }
          }

        properties = dict(row['events_tags'])
        properties.update({
            'createdate': row['createdate'],
            'lastupdate': row['lastupdate'],
            'lon': row['lon'],
            'lat': row['lat'],
            "id": row['events_id']
        })
        if "distance" in row:
            properties['distance'] = row['distance']
        return {
            "type": "Feature",
            "geometry": json.loads(row['geometry']),
            "properties": properties
        }

    def rows_to_collection(self, rows, geom_only = False):
        return {
            "type": "FeatureCollection",
            "features": [self.row_to_feature(r, geom_only) for r in rows],
            "count": len(rows)
        }


class EventsResource(BaseEvent):

    def on_get(self, req, resp):
        db = db_connect()
        cur = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT events_id, events_tags, createdate, lastupdate, st_asgeojson(geom) as geometry FROM events JOIN geo ON (hash=events_geo)")
        resp.body = dumps(self.rows_to_collection(cur.fetchall()))
        resp.status = falcon.HTTP_200


class EventResource(BaseEvent):
    def maybe_insert_geometry(self, geometry, cur):
        # insert into geo table if not existing
        cur.execute("""INSERT INTO geo (hash, geom, geom_center) SELECT *, st_centroid(geom) FROM (SELECT md5(ewkt) as hash, st_setsrid(st_geomfromewkt(ewkt),4326) as geom FROM (SELECT st_asewkt(st_geomfromgeojson( %s )) as ewkt) as g) as i ON CONFLICT DO NOTHING RETURNING hash;""", (geometry,))
        # get its id (md5 hash)
        h = cur.fetchone()
        if h is None:
            cur.execute("""SELECT md5(st_asewkt(st_geomfromgeojson( %s )));""", (geometry,))
            h = cur.fetchone()
        return h


    def relative_time(self, when, cur):
        when = when.upper().replace(' ','+')
        event_start = cur.mogrify("%s",(when,)).decode("utf-8")
        event_stop  = cur.mogrify("%s",(when,)).decode("utf-8")

        if when == 'NOW':
            event_start = "now()"
            event_stop  = "now()"
        if when == 'TODAY':
            event_start = "CURRENT_DATE"
            event_stop  = "CURRENT_DATE + INTERVAL '1 DAY'"
        if when == 'TOMORROW':
            event_start = "CURRENT_DATE + INTERVAL '1 DAY'"
            event_stop  = "CURRENT_DATE + INTERVAL '2 DAY'"
        if when == 'YESTERDAY':
            event_start = "CURRENT_DATE - INTERVAL '1 DAY'"
            event_stop  = "CURRENT_DATE"
        m = re.match('(LAST|NEXT)(YEAR|MONTH|WEEK|DAY|HOUR|MINUTE)',when)
        if m is not None:
            when = m.group(1)+'1'+m.group(2)+'S'
        m = re.match('(LAST|NEXT)([0-9]*)(YEAR|MONTH|WEEK|MINUTE|HOUR|DAY)S',when)
        if m is not None:
            if m.group(1) == 'LAST':
                event_start = "now() - INTERVAL '%s %s'" % m.group(2,3)
                event_stop  = "now()"
            else:
                event_start = "now()"
                event_stop  = "now() + INTERVAL '%s %s'" % m.group(2,3)

        return event_start, event_stop


    def on_get(self, req, resp, id=None):
        db = db_connect()
        cur = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
        if id is None:
            # get query search parameters

            if 'bbox' in req.params:
                # limit search with bbox (E,S,W,N)
                event_bbox = cur.mogrify(" AND geom && ST_SetSRID(ST_MakeBox2D(ST_Point(%s,%s),ST_Point(%s,%s)),4326) ",tuple(req.params['bbox'])).decode("utf-8")
                event_dist = ""
            elif 'near' in req.params:
                # Limit search with location+distance
                # (long, lat, distance in meters)
                if len(req.params['near']) < 3:
                    dist = 1
                else:
                    dist = req.params['near'][2]
                event_bbox = cur.mogrify(" AND ST_Intersects(geom, ST_Buffer(st_setsrid(st_makepoint(%s,%s),4326)::geography,%s)::geometry) ", (req.params['near'][0], req.params['near'][1], dist)).decode("utf-8")
                event_dist = cur.mogrify("ST_Length(ST_ShortestLine(geom, st_setsrid(st_makepoint(%s,%s),4326))::geography)::integer as distance,", (req.params['near'][0], req.params['near'][1])).decode("utf-8")
            else:
                event_bbox = ""
                event_dist = ""

            if 'when' in req.params:
                # limit search with fixed time
                when = req.params['when'].upper()
                event_when = "tstzrange(%s,%s,'[]')" % (self.relative_time(when,cur))
            elif 'start' in req.params and 'stop' in req.params:
                # limit search with fixed time (start to stop)
                event_start, unused = self.relative_time(req.params['start'],cur)
                unused, event_stop = self.relative_time(req.params['stop'],cur)
                event_when = "tstzrange(%s,%s,'[]')" % (event_start, event_stop)
            elif 'start' in req.params and 'stop' not in req.params:
                # limit search with fixed time (start to now)
                event_start, unused = self.relative_time(req.params['start'],cur)
                event_when = "tstzrange(%s,now(),'[]')" % event_start
            elif 'start' not in req.params and 'stop' in req.params:
                # limit search with fixed time (now to stop)
                unused, event_stop = self.relative_time(req.params['stop'],cur)
                event_when = "tstzrange(now(),%s,'[]')" % event_stop
            else:
                event_when = "tstzrange(now(),now(),'[]')"

            if 'what' in req.params:
                # limit search based on "what"
                event_what = cur.mogrify(" AND events_what LIKE %s ", (req.params['what']+"%",)).decode("utf-8")
            else:
                event_what = ""

            if 'type' in req.params:
                # limit search based on type (scheduled, forecast, unscheduled)
                event_type = cur.mogrify(" AND events_type = %s ", (req.params['type'],)).decode("utf-8")
            else:
                event_type = ""

            if 'limit' in req.params:
                limit = cur.mogrify("LIMIT %s", (req.params['limit'],)).decode("utf-8")
            else:
                limit = "LIMIT 200"

            event_geom = "geom_center"
            geom_only = False
            if 'geom' in req.params:
                if req.params['geom'] == 'full':
                    event_geom = "geom"
                elif req.params['geom'] == 'only':
                    geom_only = True
                else:
                    event_geom = cur.mogrify("ST_SnapToGrid(geom,%s)",(req.params['geom'],)).decode("utf-8")

            # Search recent active events.
            sql = """SELECT events_id, events_tags, createdate, lastupdate, {event_dist} st_asgeojson({event_geom}) as geometry, st_x(geom_center) as lon, st_y(geom_center) as lat FROM events JOIN geo ON (hash=events_geo) {event_bbox} WHERE events_when && {event_when} {event_what} {event_type} ORDER BY createdate DESC {limit}"""
            # No user generated content here, so format is safe.
            sql = sql.format(event_dist=event_dist, event_geom=event_geom,
                             event_bbox=event_bbox, event_what=event_what,
                             event_when=event_when, event_type=event_type, limit=limit)
            cur.execute(sql)
            resp.body = dumps(self.rows_to_collection(cur.fetchall(), geom_only))
            resp.status = falcon.HTTP_200
        else:
            # Get single event geojson Feature by id.
            cur.execute("SELECT events_id, events_tags, createdate, lastupdate, st_asgeojson(geom) as geometry, st_x(geom_center) as lon, st_y(geom_center) as lat FROM events JOIN geo ON (hash=events_geo) WHERE events_id=%s", [id])

            e = cur.fetchone()
            if e is not None:
                resp.body = dumps(self.row_to_feature(e))
                resp.status = falcon.HTTP_200
            else:
                resp.status = falcon.HTTP_404
        db.close()

    def insert_or_update(self, req, resp, id, query):

        # get request body payload (geojson Feature)
        body = req.stream.read().decode('utf-8')
        j = json.loads(body)
        if "properties" not in j or "geometry" not in j:
            resp.body = "missing 'geometry' or 'properties' elements"
            resp.status = falcon.HTTP_400
        if "start" not in j['properties']:
            event_start = j['properties']['when']
        else:
            event_start = j['properties']['start']
        if "stop" not in j['properties']:
            event_stop = j['properties']['when']
        else:
            event_stop = j['properties']['stop']
        if event_start == event_stop:
            bounds = '[]'
        else:
            bounds = '[)'
        # connect to db and insert
        db = db_connect()
        cur = db.cursor()
        # get the geometry part
        geometry=dumps(j['geometry'])
        h = self.maybe_insert_geometry(geometry,cur)
        params = (j['properties']['type'], j['properties']['what'], event_start, event_stop, bounds, dumps(j['properties']), h[0])
        if id:
            params = params + (id,)
        cur.execute(query, params)
        # get newly created event id
        e = cur.fetchone()
        db.commit()
        cur.close()
        db.close()
        # send back to client
        if e is None:
          resp.status = falcon.HTTP_409
        else:
          resp.body = """{"id":"%s"}""" % (e[0])
          resp.status = falcon.HTTP_201

    def on_post(self, req, resp):
        self.insert_or_update(req, resp, None, """INSERT INTO events ( events_type, events_what, events_when, events_tags, events_geo) VALUES (%s, %s, tstzrange(%s,%s,%s) , %s, %s) ON CONFLICT DO NOTHING RETURNING events_id;""")

    def on_put(self, req, resp, id):
        self.insert_or_update(req, resp, id, """UPDATE events SET ( events_type, events_what, events_when, events_tags, events_geo) = (%s, %s, tstzrange(%s,%s,%s) , %s, %s) WHERE events_id = %s RETURNING events_id;""")

    def on_delete(self, req, resp, id):
        db = db_connect()
        cur = db.cursor()
        cur.execute("""DELETE FROM events WHERE events_id = %s;""", (id,));
        db.commit()
        cur.close()
        db.close()
        if cur.rowcount:
            resp.status = falcon.HTTP_204
        else:
            resp.status = falcon.HTTP_404

# Falcon.API instances are callable WSGI apps.
app = falcon.API(middleware=[HeaderMiddleware()])

# Resources are represented by long-lived class instances
events = EventsResource()
event = EventResource()
stats = StatsResource()

# things will handle all requests to the matching URL path
app.add_route('/events', events)
app.add_route('/event/{id}', event)  # handle single event requests
app.add_route('/event', event)  # handle single event requests
app.add_route('/stats', stats)
