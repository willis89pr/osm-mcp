import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, AsyncIterator
from contextlib import asynccontextmanager

import psycopg2
import psycopg2.extras
from mcp.server.fastmcp import Context, FastMCP

from flask_server import FlaskServer


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


# Initialize the MCP server
mcp = FastMCP("OSM MCP Server", dependencies=["psycopg2>=2.9.10", "flask>=3.1.0"])

@dataclass
class AppContext:
    db_conn: Optional[PostgresConnection] = None
    flask_server: Optional[FlaskServer] = None

@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Manage application lifecycle with type-safe context"""
    app_ctx = AppContext()
    try:
        # Initialize database connection (optional)
        try:
            log("Connecting to database...")
            conn = psycopg2.connect(
                host=os.environ.get("POSTGRES_HOST", "localhost"),
                port=os.environ.get("POSTGRES_PORT", "5432"),
                dbname=os.environ.get("POSTGRES_DB", "osm"),
                user=os.environ.get("POSTGRES_USER", "postgres"),
                password=os.environ.get("POSTGRES_PASSWORD", "postgres"),
            )
            app_ctx.db_conn = PostgresConnection(conn)
            log("Database connection established")
        except Exception as e:
            log(f"Warning: Could not connect to database: {e}")
            log("Continuing without database connection")
        
        # Initialize and start Flask server
        log("Starting Flask server...")
        flask_server = FlaskServer(
            host=os.environ.get("FLASK_HOST", "127.0.0.1"),
            port=int(os.environ.get("FLASK_PORT", "5000"))
        )
        flask_server.start()
        app_ctx.flask_server = flask_server
        log(f"Flask server started at http://{flask_server.host}:{flask_server.port}")
        
        yield app_ctx
    finally:
        # Cleanup on shutdown
        if app_ctx.flask_server:
            log("Stopping Flask server...")
            app_ctx.flask_server.stop()
        
        if app_ctx.db_conn and app_ctx.db_conn.conn:
            log("Closing database connection...")
            app_ctx.db_conn.conn.close()

# Set the lifespan manager for the MCP server
mcp = FastMCP("OSM MCP Server", 
              dependencies=["psycopg2>=2.9.10", "flask>=3.1.0"],
              lifespan=app_lifespan)

def is_read_only_query(query: str) -> bool:
    """Check if a query is read-only."""
    # Normalize query by removing comments and extra whitespace
    query = re.sub(r"--.*$", "", query, flags=re.MULTILINE)
    query = re.sub(r"/\*.*?\*/", "", query, flags=re.DOTALL)
    query = query.strip().lower()

    # Check for write operations
    write_operations = [
        r"^\s*insert\s+",
        r"^\s*update\s+",
        r"^\s*delete\s+",
        r"^\s*drop\s+",
        r"^\s*create\s+",
        r"^\s*alter\s+",
        r"^\s*truncate\s+",
        r"^\s*grant\s+",
        r"^\s*revoke\s+",
        r"^\s*set\s+",
    ]

    for pattern in write_operations:
        if re.search(pattern, query):
            return False

    return True

# Modify the existing tools to check for database connection
@mcp.tool()
async def query_osm_postgres(query: str, ctx: Context) -> str:
    """
    Execute a SQL query against the OpenStreetMap PostgreSQL database.
    
    The query must be a valid SQL query that can be executed against the OpenStreetMap
    database schema. For security reasons, only read-only queries are allowed.
    
    Examples:
    - `SELECT * FROM planet_osm_point LIMIT 10`
    - `SELECT name, amenity FROM planet_osm_point WHERE amenity = 'restaurant' LIMIT 10`
    """
    # Check if database connection is available
    if not ctx.lifespan_context or not ctx.lifespan_context.db_conn:
        return "Database connection is not available. Please check your PostgreSQL server."
    
    enforce_read_only = True
    max_rows = 100

    if enforce_read_only and not is_read_only_query(query):
        return "Error: Only read-only queries are allowed for security reasons."

    try:
        results, total_rows = await ctx.lifespan_context.db_conn.execute_query(query, max_rows=max_rows)

        if not results:
            return "Query executed successfully, but returned no results."

        # Format results as a table
        columns = list(results[0].keys())
        rows = [[str(row.get(col, "")) for col in columns] for row in results]

        # Calculate column widths
        col_widths = [max(len(col), max([len(row[i]) for row in rows] + [0])) for i, col in enumerate(columns)]

        # Format header
        header = " | ".join(col.ljust(col_widths[i]) for i, col in enumerate(columns))
        separator = "-+-".join("-" * width for width in col_widths)

        # Format rows
        formatted_rows = [
            " | ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(row)) for row in rows
        ]

        # Combine all parts
        table = f"{header}\n{separator}\n" + "\n".join(formatted_rows)

        # Add summary
        if total_rows > max_rows:
            table += f"\n\n(Showing {len(results)} of {total_rows} rows)"

        return table
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
    # Check if database connection is available
    if not ctx.lifespan_context or not ctx.lifespan_context.db_conn:
        return "Database connection is not available. Please check your PostgreSQL server."
    
    db_conn = ctx.lifespan_context.db_conn
    tables = await db_conn.get_tables()

    schema_info = [
        "# OpenStreetMap Database Schema\n",
        "OpenStreetMap (OSM) Data Structure:\n",
        "In OpenStreetMap, data is structured using:\n",
        "1. Nodes: Points with coordinates (shops, landmarks, etc.)\n",
        "2. Ways: Lines or polygons defined by ordered lists of nodes (roads, buildings, etc.)\n",
        "3. Relations: Groups of nodes and ways (complex features like routes)\n",
        "\nDatabase Tables:\n"
    ]

    for table in tables:
        if table.startswith("planet_osm_"):
            table_info = await db_conn.get_table_info(table)

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
