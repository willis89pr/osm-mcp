import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from mcp.server.fastmcp import Context, FastMCP


def log(msg):
    print(msg, file=sys.stderr)


# Custom database connection class
@dataclass
class PostgresConnection:
    conn: Any

    async def execute_query(
        self, query: str, params: Optional[Dict[str, Any]] = None, max_rows: int = 1000
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Execute a query and return results as a list of dictionaries with total count."""
        log(f"Executing query: {query}, params: {params}")
        start_time = time.time()
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                # Set statement timeout to 5 seconds
                cur.execute("SET statement_timeout = 5000")

                if params:
                    cur.execute(query, params)
                else:
                    cur.execute(query)
                end_time = time.time()
                log(f"Query execution time: {end_time - start_time} seconds")
                total_rows = cur.rowcount
                results = cur.fetchmany(max_rows)
                log(f"Got {total_rows} rows")
                # Log first 3 rows.
                for row in results[:3]:
                    log(f"Row: {row}")
                return results, total_rows
            except psycopg2.errors.QueryCanceled:
                self.conn.rollback()
                raise TimeoutError("Query execution timed out")
            except Exception as e:
                self.conn.rollback()
                raise e

    async def get_tables(self) -> List[str]:
        """Get list of tables in the database."""
        query = """
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public'
        ORDER BY table_name;
        """
        with self.conn.cursor() as cur:
            cur.execute(query)
            return [row[0] for row in cur.fetchall()]

    async def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        """Get schema information for a table."""
        query = """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position;
        """
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (table_name,))
            return cur.fetchall()

    async def get_table_info(self, table_name: str) -> Dict[str, Any]:
        """Get detailed information about a table including indexes."""
        # Get table columns
        columns = await self.get_table_schema(table_name)
        # Get table indexes
        index_query = """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE tablename = %s;
        """
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(index_query, (table_name,))
            indexes = cur.fetchall()
        # Get table row count (approximate)
        count_query = f"SELECT count(*) FROM {table_name};"
        with self.conn.cursor() as cur:
            cur.execute(count_query)
            row_count = cur.fetchone()[0]
        return {
            "name": table_name,
            "columns": columns,
            "indexes": indexes,
            "approximate_row_count": row_count,
        }


# SQL validation function
def is_read_only_query(query: str) -> bool:
    """Check if a query is read-only (SELECT, EXPLAIN, etc.)."""
    # Normalize query: remove comments and extra whitespace
    clean_query = re.sub(r"--.*?$", "", query, flags=re.MULTILINE)
    clean_query = re.sub(r"/\*.*?\*/", "", clean_query, flags=re.DOTALL)
    clean_query = clean_query.strip()
    # Check if query starts with SELECT, EXPLAIN, SHOW, etc.
    read_ops = ["select", "explain", "show", "with"]
    first_word = clean_query.split()[0].lower() if clean_query.split() else ""
    return first_word in read_ops and "into" not in clean_query.lower()


# Get connection parameters from environment variables
pg_params = {
    "dbname": os.environ.get("PGDATABASE", "osm"),
    "user": os.environ.get("PGUSER", "wiseman"),
    "host": os.environ.get("PGHOST", "localhost"),
    "port": os.environ.get("PGPORT", "5432"),
    "password": os.environ.get("PGPASSWORD", None),
}
# print(f"Connecting to PostgreSQL database: {pg_params}")
pg_conn = psycopg2.connect(**pg_params)
db = PostgresConnection(pg_conn)


# Create MCP server
mcp = FastMCP("OSM PostgreSQL Server")

# Describe OSM data structure
OSM_DESCRIPTION = """
OpenStreetMap (OSM) Data Structure:

In OpenStreetMap, data is structured using:

1. Nodes: Points with coordinates (shops, landmarks, etc.)
2. Ways: Lines or polygons defined by ordered lists of nodes (roads, buildings, etc.)
3. Relations: Groups of nodes and ways (complex features like routes)

The main tables in this database are:
- planet_osm_point: Point features
- planet_osm_line: Linear features
- planet_osm_polygon: Area features
- planet_osm_rels: Relations between features
- planet_osm_roads: Simplified road network
- planet_osm_ways: Raw way data

Each feature has 'tags' (key-value pairs) that describe its properties.
Common tag keys are stored as individual columns (name, highway, building, etc.).
Additional tags are stored in the 'tags' column as an hstore type.

The 'way' column contains the geometry in SRID 4326 (WGS84) format.
"""


# Tool to execute SQL queries
@mcp.tool()
async def query_osm_postgres(query: str, ctx: Context) -> str:
    """
        Execute SQL query against the OSM PostgreSQL database. This database
        contains the complete OSM data in a postgres database, and is an excellent
        way to analyze or query geospatial/geographic data.

        Args:
            query: SQL query to execute

        Returns:
            Query results as formatted text

    Example query: Find points of interest near a location
    ```sql
    SELECT osm_id, name, amenity, tourism, shop, tags
    FROM planet_osm_point
    WHERE (amenity IS NOT NULL OR tourism IS NOT NULL OR shop IS NOT NULL)
      AND ST_DWithin(
          geography(way),
          geography(ST_SetSRID(ST_MakePoint(-73.99, 40.71), 4326)),
          1000  -- 1000 meters
      );
    ```

    The database is in postgres using the postgis extension. It was
    created by the osm2pgsql tool. This database is a complete dump of the
    OSM data.

    In OpenStreetMap (OSM), data is structured using nodes (points), ways
    (lines/polygons), and relations. Nodes represent individual points
    with coordinates, while ways are ordered lists of nodes forming lines
    or closed shapes (polygons).

    Remember that name alone is not sufficient to disambiguate a
    feature. For any name you can think of, there are dozens of features
    around the world with that name, probably even of the same type
    (e.g. lots of cities named "Los Angeles"). If you know the general
    location, you can use a bounding box to disambiguate.

    Always try to get and refer to OSM IDs when possible because they are
    unique and are the absolute fastest way to refer again to a
    feature. Users don't usually care what they are but they can help you
    speed up subsequent queries.

    Speaking of speed, there's a TON of data, so queries that don't use
    indexes will be too slow. It's usually best to use postgres and
    postgis functions, and advanced sql when possible. If you need to
    explore the data to get a sense of tags, etc., make sure to limit the
    number of rows you get back to a small number or use aggregation
    functions. Every query will either need to be filtered with WHERE
    clauses or be an aggregation query.

    IMPORTANT: All the spatial indexes are on the geography type, not the
    geometry type. This means if you do a spatial query, you need to use
    the geography function. For example:

    ```
    SELECT
        b.osm_id AS building_id,
        b.name AS building_name,
        ST_AsText(b.way) AS building_geometry
    FROM
        planet_osm_polygon b
    JOIN
        planet_osm_polygon burbank ON burbank.osm_id = -3529574
    JOIN
        planet_osm_polygon glendale ON glendale.osm_id = -2313082
    WHERE
        ST_Intersects(b.way::geography, burbank.way::geography) AND
        ST_Intersects(b.way::geography, glendale.way::geography) AND
        b.building IS NOT NULL;
    ```

    Here's a more detailed explanation of the data representation:

    • Nodes: [1, 2, 3]
            • Represent individual points on the map with latitude and
              longitude coordinates. [1, 2, 3]
            • Can be used to represent point features like shops, lamp
              posts, etc. [1]
            • Collections of nodes are also used to define the shape of
              ways. [1]

    • Ways: [1, 2]
            • Represent collections of nodes. [1, 2]
            • Do not store their own coordinates; instead, they store an ordered
              list of node identifiers. [1, 2]

            • Ways can be open (lines) or closed (polygons). [2, 5]

            • Used to represent various features like roads, railways, river
              centerlines, powerlines, and administrative borders. [1]

    • Relations: [4]
            • Are groups of nodes and/or ways, used to represent complex features
              like routes, areas, or relationships between map elements. [4]

    [1] https://algo.win.tue.nl/tutorials/openstreetmap/
    [2] https://docs.geodesk.com/intro-to-osm
    [3] https://wiki.openstreetmap.org/wiki/Elements
    [4] https://racum.blog/articles/osm-to-geojson/
    [5] https://wiki.openstreetmap.org/wiki/Way

    Tags are key-value pairs that describe the features in the map. They
    are used to store information about the features, such as their name,
    type, or other properties. Note that in the following tables, some
    tags have their own columns, but all other tags are stored in the tags
    column as a hstore type.

    List of tables:
    | Name               |
    |--------------------|
    | planet_osm_line    |
    | planet_osm_point   |
    | planet_osm_polygon |
    | planet_osm_rels    |
    | planet_osm_roads   |
    | planet_osm_ways    |
    | spatial_ref_sys    |

    Table "public.planet_osm_line":
    | Column             | Type                      |
    |--------------------+---------------------------|
    | osm_id             | bigint                    |
    | access             | text                      |
    | addr:housename     | text                      |
    | addr:housenumber   | text                      |
    | addr:interpolation | text                      |
    | admin_level        | text                      |
    | aerialway          | text                      |
    | aeroway            | text                      |
    | amenity            | text                      |
    | area               | text                      |
    | barrier            | text                      |
    | bicycle            | text                      |
    | brand              | text                      |
    | bridge             | text                      |
    | boundary           | text                      |
    | building           | text                      |
    | construction       | text                      |
    | covered            | text                      |
    | culvert            | text                      |
    | cutting            | text                      |
    | denomination       | text                      |
    | disused            | text                      |
    | embankment         | text                      |
    | foot               | text                      |
    | generator:source   | text                      |
    | harbour            | text                      |
    | highway            | text                      |
    | historic           | text                      |
    | horse              | text                      |
    | intermittent       | text                      |
    | junction           | text                      |
    | landuse            | text                      |
    | layer              | text                      |
    | leisure            | text                      |
    | lock               | text                      |
    | man_made           | text                      |
    | military           | text                      |
    | motorcar           | text                      |
    | name               | text                      |
    | natural            | text                      |
    | office             | text                      |
    | oneway             | text                      |
    | operator           | text                      |
    | place              | text                      |
    | population         | text                      |
    | power              | text                      |
    | power_source       | text                      |
    | public_transport   | text                      |
    | railway            | text                      |
    | ref                | text                      |
    | religion           | text                      |
    | route              | text                      |
    | service            | text                      |
    | shop               | text                      |
    | sport              | text                      |
    | surface            | text                      |
    | toll               | text                      |
    | tourism            | text                      |
    | tower:type         | text                      |
    | tracktype          | text                      |
    | tunnel             | text                      |
    | water              | text                      |
    | waterway           | text                      |
    | wetland            | text                      |
    | width              | text                      |
    | wood               | text                      |
    | z_order            | integer                   |
    | way_area           | real                      |
    | tags               | hstore                    |
    | way                | geometry(LineString,4326) |
    Indexes:
        "planet_osm_line_osm_id_idx" btree (osm_id)
        "planet_osm_line_tags_idx" gin (tags)
        "planet_osm_line_way_geog_idx" gist (geography(way))

    Table "public.planet_osm_point":
    | Column             | Type                 |
    |--------------------+----------------------|
    | osm_id             | bigint               |
    | access             | text                 |
    | addr:housename     | text                 |
    | addr:housenumber   | text                 |
    | addr:interpolation | text                 |
    | admin_level        | text                 |
    | aerialway          | text                 |
    | aeroway            | text                 |
    | amenity            | text                 |
    | area               | text                 |
    | barrier            | text                 |
    | bicycle            | text                 |
    | brand              | text                 |
    | bridge             | text                 |
    | boundary           | text                 |
    | building           | text                 |
    | capital            | text                 |
    | construction       | text                 |
    | covered            | text                 |
    | culvert            | text                 |
    | cutting            | text                 |
    | denomination       | text                 |
    | disused            | text                 |
    | ele                | text                 |
    | embankment         | text                 |
    | foot               | text                 |
    | generator:source   | text                 |
    | harbour            | text                 |
    | highway            | text                 |
    | historic           | text                 |
    | horse              | text                 |
    | intermittent       | text                 |
    | junction           | text                 |
    | landuse            | text                 |
    | layer              | text                 |
    | leisure            | text                 |
    | lock               | text                 |
    | man_made           | text                 |
    | military           | text                 |
    | motorcar           | text                 |
    | name               | text                 |
    | natural            | text                 |
    | office             | text                 |
    | oneway             | text                 |
    | operator           | text                 |
    | place              | text                 |
    | population         | text                 |
    | power              | text                 |
    | power_source       | text                 |
    | public_transport   | text                 |
    | railway            | text                 |
    | ref                | text                 |
    | religion           | text                 |
    | route              | text                 |
    | service            | text                 |
    | shop               | text                 |
    | sport              | text                 |
    | surface            | text                 |
    | toll               | text                 |
    | tourism            | text                 |
    | tower:type         | text                 |
    | tunnel             | text                 |
    | water              | text                 |
    | waterway           | text                 |
    | wetland            | text                 |
    | width              | text                 |
    | wood               | text                 |
    | z_order            | integer              |
    | tags               | hstore               |
    | way                | geometry(Point,4326) |
    Indexes:
        "planet_osm_point_osm_id_idx" btree (osm_id)
        "planet_osm_point_tags_idx" gin (tags)
        "planet_osm_point_way_geog_idx" gist (geography(way))

    Table "public.planet_osm_polygon":
    | Column             | Type                    |
    |--------------------+-------------------------|
    | osm_id             | bigint                  |
    | access             | text                    |
    | addr:housename     | text                    |
    | addr:housenumber   | text                    |
    | addr:interpolation | text                    |
    | admin_level        | text                    |
    | aerialway          | text                    |
    | aeroway            | text                    |
    | amenity            | text                    |
    | area               | text                    |
    | barrier            | text                    |
    | bicycle            | text                    |
    | brand              | text                    |
    | bridge             | text                    |
    | boundary           | text                    |
    | building           | text                    |
    | construction       | text                    |
    | covered            | text                    |
    | culvert            | text                    |
    | cutting            | text                    |
    | denomination       | text                    |
    | disused            | text                    |
    | embankment         | text                    |
    | foot               | text                    |
    | generator:source   | text                    |
    | harbour            | text                    |
    | highway            | text                    |
    | historic           | text                    |
    | horse              | text                    |
    | intermittent       | text                    |
    | junction           | text                    |
    | landuse            | text                    |
    | layer              | text                    |
    | leisure            | text                    |
    | lock               | text                    |
    | man_made           | text                    |
    | military           | text                    |
    | motorcar           | text                    |
    | name               | text                    |
    | natural            | text                    |
    | office             | text                    |
    | oneway             | text                    |
    | operator           | text                    |
    | place              | text                    |
    | population         | text                    |
    | power              | text                    |
    | power_source       | text                    |
    | public_transport   | text                    |
    | railway            | text                    |
    | ref                | text                    |
    | religion           | text                    |
    | route              | text                    |
    | service            | text                    |
    | shop               | text                    |
    | sport              | text                    |
    | surface            | text                    |
    | toll               | text                    |
    | tourism            | text                    |
    | tower:type         | text                    |
    | tracktype          | text                    |
    | tunnel             | text                    |
    | water              | text                    |
    | waterway           | text                    |
    | wetland            | text                    |
    | width              | text                    |
    | wood               | text                    |
    | z_order            | integer                 |
    | way_area           | real                    |
    | tags               | hstore                  |
    | way                | geometry(Geometry,4326) |
    Indexes:
        "planet_osm_polygon_osm_id_idx" btree (osm_id)
        "planet_osm_polygon_tags_idx" gin (tags)
        "planet_osm_polygon_way_geog_idx" gist (geography(way))

    Table "public.planet_osm_rels":
    | Column  | Type     |
    |---------+----------|
    | id      | bigint   |
    | way_off | smallint |
    | rel_off | smallint |
    | parts   | bigint[] |
    | members | text[]   |
    | tags    | text[]   |
    Indexes:
        "planet_osm_rels_pkey" PRIMARY KEY, btree (id)
        "planet_osm_rels_parts_idx" gin (parts) WITH (fastupdate=off)
    """
    enforce_read_only = True
    max_rows = 100

    # Check if query is read-only if enforcement is enabled
    if enforce_read_only and not is_read_only_query(query):
        return "Error: Only read-only queries (SELECT, EXPLAIN, etc.) are allowed. Please modify your query."

    try:
        results, total_rows = await db.execute_query(query, max_rows=max_rows)

        if not results:
            return "Query executed successfully, but returned no results."

        # Format results as text
        result_str = json.dumps(results, indent=2, default=str)

        # Add information about truncated results
        if total_rows > len(results):
            result_str += f"\n\nNote: Showing {len(results)} of {total_rows} total rows. Use LIMIT in your query for more control."

        return result_str
    except Exception as e:
        return f"Error executing query: {str(e)}"


def find_features_by_name_sql(name: str, feature_type: str) -> str:
    # Generate the SQL used by the find_features_by_name tool.
    table_name = f"planet_osm_{feature_type}"
    query = f"""
    SELECT osm_id, name, ST_AsText(ST_Centroid(way)) AS centroid
    FROM {table_name}
    WHERE name = %(name)s
    LIMIT %(limit)s
    """
    return query


def find_features_near_location_sql(
    longitude: float,
    latitude: float,
    radius_meters: float,
    feature_type: str,
    feature_filters: Dict[str, str],
) -> str:
    # Generate the SQL used by the find_features_near_location tool.
    if not -180 <= longitude <= 180:
        raise ValueError("Longitude must be between -180 and 180")
    if not -90 <= latitude <= 90:
        raise ValueError("Latitude must be between -90 and 90")
    if radius_meters <= 0 or radius_meters > 10000:
        raise ValueError("Radius must be between 0 and 10000 meters")
    valid_types = ["point", "line", "polygon"]
    if feature_type not in valid_types:
        raise ValueError("Invalid feature type. Choose from: point, line, polygon.")
    table_name = f"planet_osm_{feature_type}"
    filters = []
    for key, value in feature_filters.items():
        if key == "tags":
            for tag_key, tag_value in value.items():
                filters.append(f"tags->'{tag_key}' = '{tag_value}'")
        else:
            filters.append(f"{key} = '{value}'")
    filter_clause = " AND ".join(filters) if filters else "TRUE"
    query = f"""
    SELECT osm_id, name, ST_AsText(ST_Centroid(way)) AS centroid
    FROM {table_name}
    WHERE ST_DWithin(
        geography(way),
        geography(ST_SetSRID(ST_MakePoint(%s, %s), 4326)),
        %s
    ) AND {filter_clause}
    ORDER BY distance_meters
    LIMIT %s
    """
    return query


# Resource for database schema information
@mcp.resource("osm://schema{ctx}")
async def get_schema_info(ctx: Context) -> str:
    """
    Get information about the OSM database schema
    """
    tables = await db.get_tables()

    schema_info = [OSM_DESCRIPTION, "\nDatabase Tables:\n"]

    for table in tables:
        if table.startswith("planet_osm_"):
            table_info = await db.get_table_info(table)

            schema_info.append(f"\n## {table}")
            schema_info.append(
                f"Approximate row count: {table_info['approximate_row_count']}"
            )

            schema_info.append("\nColumns:")
            for col in table_info["columns"]:
                nullable = "NULL" if col["is_nullable"] == "YES" else "NOT NULL"
                schema_info.append(
                    f"- {col['column_name']} ({col['data_type']}) {nullable}"
                )

            schema_info.append("\nIndexes:")
            for idx in table_info["indexes"]:
                schema_info.append(f"- {idx['indexname']}: {idx['indexdef']}")

    return "\n".join(schema_info)


# Resource for common query examples
@mcp.resource("osm://query-examples")
def get_query_examples() -> str:
    """
    Provide examples of common OSM database queries
    """
    return """
# Common OSM PostgreSQL Query Examples

## Find all restaurants in an area
```sql
SELECT osm_id, name, amenity, cuisine, tags
FROM planet_osm_point
WHERE amenity = 'restaurant'
  AND ST_Contains(
      ST_MakeEnvelope(-74.01, 40.70, -73.97, 40.73, 4326),
      way
  );
```

## Find all highways in an area
```sql
SELECT osm_id, name, highway, way
FROM planet_osm_line
WHERE highway IS NOT NULL
  AND ST_Contains(
      ST_MakeEnvelope(-74.01, 40.70, -73.97, 40.73, 4326),
      way
  );
```

## Find all buildings in an area
```sql
SELECT osm_id, name, building, way
FROM planet_osm_polygon
WHERE building IS NOT NULL
  AND ST_Contains(
      ST_MakeEnvelope(-74.01, 40.70, -73.97, 40.73, 4326),
      way
  );
```

## Find points of interest near a location
```sql
SELECT osm_id, name, amenity, tourism, shop, tags
FROM planet_osm_point
WHERE (amenity IS NOT NULL OR tourism IS NOT NULL OR shop IS NOT NULL)
  AND ST_DWithin(
      geography(way),
      geography(ST_SetSRID(ST_MakePoint(-73.99, 40.71), 4326)),
      1000  -- 1000 meters
  );
```

## Count features by type
```sql
SELECT 'points' AS type, COUNT(*) FROM planet_osm_point
UNION ALL
SELECT 'lines' AS type, COUNT(*) FROM planet_osm_line
UNION ALL
SELECT 'polygons' AS type, COUNT(*) FROM planet_osm_polygon;
```

## Find all parks
```sql
SELECT osm_id, name, leisure, way
FROM planet_osm_polygon
WHERE leisure = 'park';
```

## Find all water bodies
```sql
SELECT osm_id, name, "natural", water, waterway, way
FROM planet_osm_polygon
WHERE "natural" = 'water' OR water IS NOT NULL;
```

## Find streets with a specific name
```sql
SELECT osm_id, name, highway, way
FROM planet_osm_line
WHERE name ILIKE '%broadway%'
  AND highway IS NOT NULL;
```

## Find all schools
```sql
SELECT osm_id, name, amenity, way
FROM planet_osm_polygon
WHERE amenity = 'school';
```

## Find all subway/metro stations
```sql
SELECT osm_id, name, railway, way
FROM planet_osm_point
WHERE railway = 'station'
  AND tags->'station' = 'subway';
```

## Get a list of all highways by type
```sql
SELECT highway, COUNT(*) as count
FROM planet_osm_line
WHERE highway IS NOT NULL
GROUP BY highway
ORDER BY count DESC;
```

## Find streets with the most lanes
```sql
SELECT name, highway, tags->'lanes' AS lanes
FROM planet_osm_line
WHERE tags ? 'lanes'
ORDER BY (tags->'lanes')::integer DESC
LIMIT 10;
```
"""


# Add information about the hstore data type
@mcp.resource("osm://hstore-usage")
def get_hstore_info() -> str:
    """
    Provide information about using the hstore data type for tags
    """
    return """
# Working with hstore tags in OSM PostgreSQL

The OSM database stores additional tags in an hstore column called 'tags'.
Hstore is a key-value store data type in PostgreSQL.

## Access specific tag values
```sql
-- Get the value of the 'opening_hours' tag
SELECT name, tags->'opening_hours' AS opening_hours
FROM planet_osm_point
WHERE amenity = 'restaurant'
  AND tags ? 'opening_hours';
```

## Filter by tag existence
```sql
-- Find features with a specific tag
SELECT osm_id, name
FROM planet_osm_point
WHERE tags ? 'wheelchair';
```

## Filter by tag value
```sql
-- Find features with a specific tag value
SELECT osm_id, name
FROM planet_osm_point
WHERE tags @> 'wheelchair=>yes';
```

## Get all keys
```sql
-- Get all keys from the tags column
SELECT DISTINCT skeys(tags) AS tag_key
FROM planet_osm_point
ORDER BY tag_key;
```

## Get key-value pairs as rows
```sql
-- Get key-value pairs as rows
SELECT name, (each(tags)).key, (each(tags)).value
FROM planet_osm_point
WHERE amenity = 'restaurant'
LIMIT 10;
```

## Find features with multiple tag conditions
```sql
-- Find restaurants with wheelchair access and outdoor seating
SELECT name, amenity, tags
FROM planet_osm_point
WHERE amenity = 'restaurant'
  AND tags @> 'wheelchair=>yes'
  AND tags @> 'outdoor_seating=>yes';
```

## Find features with specific tag patterns
```sql
-- Find features with tag keys starting with 'addr:'
SELECT osm_id, name, tags
FROM planet_osm_point
WHERE EXISTS (
    SELECT 1
    FROM EACH(tags) AS t
    WHERE t.key LIKE 'addr:%'
);
```

## Count features by tag value
```sql
-- Count restaurants by cuisine
SELECT tags->'cuisine' AS cuisine, COUNT(*) AS count
FROM planet_osm_point
WHERE amenity = 'restaurant'
  AND tags ? 'cuisine'
GROUP BY tags->'cuisine'
ORDER BY count DESC;
```
"""


# Resource providing information about spatial queries
@mcp.resource("osm://spatial-queries")
def get_spatial_queries_info() -> str:
    """
    Provide information about performing spatial queries with PostGIS
    """
    return """
# Spatial Queries with PostGIS in OSM Database

PostGIS provides powerful spatial functions for querying OSM data.

## Find features within a bounding box
```sql
SELECT name, amenity
FROM planet_osm_point
WHERE ST_Contains(
    ST_MakeEnvelope(
        -74.01, 40.70,  -- min longitude, min latitude
        -73.97, 40.73,  -- max longitude, max latitude
        4326            -- SRID: WGS84
    ),
    way
);
```

## Find features within a distance of a point
```sql
SELECT name, amenity, ST_Distance(
    geography(way),
    geography(ST_SetSRID(ST_MakePoint(-73.99, 40.71), 4326))
) AS distance_meters
FROM planet_osm_point
WHERE ST_DWithin(
    geography(way),
    geography(ST_SetSRID(ST_MakePoint(-73.99, 40.71), 4326)),
    500  -- 500 meters
)
ORDER BY distance_meters;
```

## Find intersections between lines
```sql
SELECT a.name AS road1, b.name AS road2, ST_AsText(ST_Intersection(a.way, b.way)) AS intersection_point
FROM planet_osm_line a, planet_osm_line b
WHERE a.highway IS NOT NULL
  AND b.highway IS NOT NULL
  AND a.osm_id < b.osm_id
  AND ST_Intersects(a.way, b.way)
LIMIT 10;
```

## Calculate area of polygons
```sql
SELECT name, landuse, ST_Area(geography(way)) AS area_sq_meters
FROM planet_osm_polygon
WHERE landuse IS NOT NULL
ORDER BY area_sq_meters DESC
LIMIT 10;
```

## Calculate length of lines
```sql
SELECT name, highway, ST_Length(geography(way)) AS length_meters
FROM planet_osm_line
WHERE highway IS NOT NULL
ORDER BY length_meters DESC
LIMIT 10;
```

## Find closest features to a point
```sql
SELECT name, amenity, ST_Distance(
    geography(way),
    geography(ST_SetSRID(ST_MakePoint(-73.99, 40.71), 4326))
) AS distance_meters
FROM planet_osm_point
WHERE amenity IS NOT NULL
ORDER BY distance_meters
LIMIT 10;
```

## Find features along a route
```sql
-- Create a linestring representing a route
WITH route AS (
    SELECT ST_MakeLine(
        ST_SetSRID(ST_MakePoint(-73.99, 40.71), 4326),
        ST_SetSRID(ST_MakePoint(-73.97, 40.73), 4326)
    ) AS geom
)
SELECT name, amenity, ST_Distance(
    geography(way),
    geography(route.geom)
) AS distance_meters
FROM planet_osm_point, route
WHERE amenity IS NOT NULL
  AND ST_DWithin(
    geography(way),
    geography(route.geom),
    100  -- 100 meters from route
  )
ORDER BY distance_meters
LIMIT 10;
```

## Create a buffer around features
```sql
-- Find all features within 100 meters of parks
WITH park_buffers AS (
    SELECT ST_Buffer(geography(way), 100)::geometry AS buffer
    FROM planet_osm_polygon
    WHERE leisure = 'park'
)
SELECT p.name, p.amenity
FROM planet_osm_point p, park_buffers b
WHERE ST_Intersects(p.way, b.buffer)
  AND p.amenity IS NOT NULL;
```

## Extract centroids of polygons
```sql
SELECT name, ST_AsText(ST_Centroid(way)) AS centroid
FROM planet_osm_polygon
WHERE building = 'yes'
LIMIT 10;
```
"""


# Resource for OSM tag key descriptions
@mcp.resource("osm://tag-descriptions")
def get_tag_descriptions() -> str:
    """
    Provide descriptions of common OSM tag keys
    """
    return """
# Common OpenStreetMap Tag Keys

## General Tags
- **name**: The name of the feature
- **ref**: Reference number or code
- **addr:housenumber**: House number
- **addr:street**: Street name
- **addr:city**: City name
- **addr:postcode**: Postal code

## Points of Interest
- **amenity**: Facilities used by visitors and residents (restaurants, schools, etc.)
  - Common values: restaurant, cafe, school, hospital, bank, parking
- **shop**: Retail shops and services
  - Common values: supermarket, convenience, clothes, bakery
- **tourism**: Tourism-related features
  - Common values: hotel, museum, attraction, viewpoint
- **leisure**: Recreational facilities and areas
  - Common values: park, garden, playground, sports_centre
- **historic**: Historic sites and monuments
  - Common values: castle, monument, archaeological_site
- **office**: Office types and businesses
  - Common values: company, government, insurance

## Transportation
- **highway**: Roads and paths
  - Common values: motorway, primary, secondary, residential, footway
- **railway**: Railway infrastructure
  - Common values: rail, station, subway, tram
- **public_transport**: Public transport facilities
  - Common values: stop_position, platform, station
- **aeroway**: Aviation facilities
  - Common values: aerodrome, terminal, runway
- **barrier**: Physical barriers
  - Common values: fence, wall, gate
- **bridge**: Bridge structures
  - Common values: yes, viaduct, aqueduct
- **tunnel**: Tunnel structures
  - Common values: yes, building_passage, culvert

## Natural Features
- **natural**: Natural physical features
  - Common values: water, wood, coastline, beach
- **water**: Type of water body
  - Common values: river, lake, pond, canal
- **waterway**: Linear water features
  - Common values: river, stream, canal, drain
- **landuse**: Primary use of land
  - Common values: residential, forest, commercial, farmland
- **surface**: Surface material
  - Common values: asphalt, gravel, grass, paved

## Properties and Attributes
- **building**: Building types
  - Common values: yes, house, apartments, commercial
- **layer**: Vertical position relative to other features
  - Values: ... -2, -1, 0, 1, 2 ...
- **height**: Height of feature
- **width**: Width of feature
- **ele**: Elevation above sea level
- **oneway**: Traffic flow direction
  - Values: yes, no, -1
"""


# Start the server if run directly
if __name__ == "__main__":
    mcp.run()
