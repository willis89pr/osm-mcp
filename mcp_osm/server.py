import logging
import os
import re
import sys
import time
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, AsyncIterator
from contextlib import asynccontextmanager

import psycopg2
import psycopg2.extras
from mcp.server.fastmcp import Context, FastMCP

from mcp_osm.flask_server import FlaskServer


# Configure all logging to stderr
logging.basicConfig(
    stream=sys.stderr,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


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
                # Set statement timeout to 10 seconds
                cur.execute("SET statement_timeout = 10000")

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
                host=os.environ.get("PGHOST", "localhost"),
                port=os.environ.get("PGPORT", "5432"),
                dbname=os.environ.get("PGDB", "osm"),
                user=os.environ.get("PGUSER", "postgres"),
                password=os.environ.get("PGPASSWORD", "postgres"),
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
            port=int(os.environ.get("FLASK_PORT","8888"))
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


# Initialize the MCP server
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


# Database query tools
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
    location, you can use a bounding box to disambiguate. YOU MUST
    DISAMBIGUATE FEATURES with bounding boxes!!!!!!!!!!!!

    Even if you have other WHERE clauses, you MUST use a bounding box to
    disambiguate features. Name and other tags alone are not sufficient.

    PostGIS has useful features like ST_Simplify which is especially
    helpful to reduce data to a reasonable size when doing visualizations.

    Always try to get and refer to OSM IDs when possible because they are
    unique and are the absolute fastest way to refer again to a
    feature. Users don't usually care what they are but they can help you
    speed up subsequent queries.

    YOU MUST DISAMBIGUATE FEATURES with bounding boxes!!!!!!!!!!!!

    Speaking of speed, there's a TON of data, so queries that don't use
    indexes will be too slow. It's usually best to use postgres and
    postgis functions, and advanced sql when possible. If you need to
    explore the data to get a sense of tags, etc., make sure to limit the
    number of rows you get back to a small number or use aggregation
    functions. Every query will either need to be filtered with WHERE
    clauses or be an aggregation query.

    YOU MUST DISAMBIGUATE FEATURES with bounding boxes!!!!!!!!!!!!

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

    [1] https://algo.win.tue.nl/tutorials/openstreetmap/
    [2] https://docs.geodesk.com/intro-to-osm
    [3] https://wiki.openstreetmap.org/wiki/Elements
    [4] https://racum.blog/articles/osm-to-geojson/
    [5] https://wiki.openstreetmap.org/wiki/Way

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
    # Check if database connection is available
    if not ctx.request_context.lifespan_context.db_conn:
        return "Database connection is not available. Please check your PostgreSQL server."
    
    enforce_read_only = True
    max_rows = 100

    if enforce_read_only and not is_read_only_query(query):
        return "Error: Only read-only queries are allowed for security reasons."

    try:
        results, total_rows = await ctx.request_context.lifespan_context.db_conn.execute_query(query, max_rows=max_rows)

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


# Map control tools
@mcp.tool()
async def set_map_view(
    ctx: Context,
    center: Optional[List[float]] = None,
    zoom: Optional[int] = None,
    bounds: Optional[List[List[float]]] = None
) -> str:
    """
    Set the map view in the web interface.
    
    Args:
        center: [latitude, longitude] center point
        zoom: Zoom level (0-19)
        bounds: [[south, west], [north, east]] bounds to display
        
    Examples:
        - Set view to a specific location: `set_map_view(center=[37.7749, -122.4194], zoom=12)`
        - Set view to show a region: `set_map_view(bounds=[[37.7, -122.5], [37.8, -122.4]])`
    """
    if not ctx.request_context.lifespan_context.flask_server:
        return "Map server is not available."
    
    # Validate parameters
    if center and (len(center) != 2 or not all(isinstance(c, (int, float)) for c in center)):
        return "Error: center must be a [latitude, longitude] pair of numbers."
    
    if zoom and (not isinstance(zoom, int) or zoom < 0 or zoom > 19):
        return "Error: zoom must be an integer between 0 and 19."
    
    if bounds:
        if (len(bounds) != 2 or len(bounds[0]) != 2 or len(bounds[1]) != 2 or 
            not all(isinstance(c, (int, float)) for point in bounds for c in point)):
            return "Error: bounds must be [[south, west], [north, east]] coordinates."
    
    # At least one parameter must be provided
    if not center and zoom is None and not bounds:
        return "Error: at least one of center, zoom, or bounds must be provided."
    
    # Send the command to the map
    server = ctx.request_context.lifespan_context.flask_server
    server.set_view(bounds=bounds, center=center, zoom=zoom)
    
    # Generate success message
    message_parts = []
    if bounds:
        message_parts.append(f"bounds={bounds}")
    if center:
        message_parts.append(f"center={center}")
    if zoom is not None:
        message_parts.append(f"zoom={zoom}")
    
    return f"Map view updated successfully: {', '.join(message_parts)}"

@mcp.tool()
async def set_map_title(
    ctx: Context,
    title: str,
    color: Optional[str] = None,
    font_size: Optional[str] = None,
    background_color: Optional[str] = None
) -> str:
    """
    Set the title displayed at the bottom right of the map.
    
    Args:
        title: Text to display as the map title
        color: CSS color value for the text (e.g., "#0066cc", "red")
        font_size: CSS font size (e.g., "24px", "1.5em")
        background_color: CSS background color value (e.g., "rgba(255, 255, 255, 0.8)")
        
    Examples:
        - Set a basic title: `set_map_title("OpenStreetMap Viewer")`
        - Set a styled title: `set_map_title("San Francisco", color="#0066cc", font_size="28px")`
    """
    if not ctx.request_context.lifespan_context.flask_server:
        return "Map server is not available."
    
    # Prepare options dictionary with only provided values
    options = {}
    if color:
        options["color"] = color
    if font_size:
        options["fontSize"] = font_size
    if background_color:
        options["backgroundColor"] = background_color
    
    # Send the command to the map
    server = ctx.request_context.lifespan_context.flask_server
    server.set_title(title, options)
    
    # Generate success message
    style_info = ""
    if options:
        style_parts = []
        if color:
            style_parts.append(f"color: {color}")
        if font_size:
            style_parts.append(f"size: {font_size}")
        if background_color:
            style_parts.append(f"background: {background_color}")
        style_info = f" with {', '.join(style_parts)}"
    
    return f"Map title set to '{title}'{style_info}"

@mcp.tool()
async def add_map_marker(
    ctx: Context,
    coordinates: List[float],
    text: Optional[str] = None,
    title: Optional[str] = None,
    open_popup: bool = False
) -> str:
    """
    Add a marker to the map at the specified coordinates.
    
    Args:
        coordinates: [latitude, longitude] location for the marker
        text: Text to display in a popup when the marker is clicked
        title: Tooltip text displayed on hover (optional)
        open_popup: Whether to automatically open the popup (default: False)
        
    Examples:
        - Add a simple marker: `add_map_marker([37.7749, -122.4194])`
        - Add a marker with popup: `add_map_marker([37.7749, -122.4194], text="San Francisco", open_popup=True)`
    """
    if not ctx.request_context.lifespan_context.flask_server:
        return "Map server is not available."
    
    # Validate coordinates
    if len(coordinates) != 2 or not all(isinstance(c, (int, float)) for c in coordinates):
        return "Error: coordinates must be a [latitude, longitude] pair of numbers."
    
    # Prepare options
    options = {}
    if title:
        options["title"] = title
    options["openPopup"] = open_popup
    
    # Send the command to the map
    server = ctx.request_context.lifespan_context.flask_server
    server.show_marker(coordinates, text, options)
    
    # Generate success message
    details = []
    if text:
        details.append(f"text: '{text}'")
    if title:
        details.append(f"title: '{title}'")
    details_str = f" with {', '.join(details)}" if details else ""
    
    return f"Marker added at coordinates [{coordinates[0]}, {coordinates[1]}]{details_str}"

@mcp.tool()
async def add_map_polygon(
    ctx: Context,
    coordinates: List[List[float]],
    color: Optional[str] = None,
    fill_color: Optional[str] = None,
    fill_opacity: Optional[float] = None,
    weight: Optional[int] = None,
    fit_bounds: bool = False
) -> str:
    """
    Add a polygon to the map with the specified coordinates.
    
    If you're trying to add a polygon with more than 20 points, stop and use
    ST_Simplify to reduce the number of points.

    Args:
        coordinates: List of [latitude, longitude] points defining the polygon
        color: Border color (CSS color value)
        fill_color: Fill color (CSS color value)
        fill_opacity: Fill opacity (0.0 to 1.0)
        weight: Border width in pixels
        fit_bounds: Whether to zoom the map to show the entire polygon
        
    Examples:
        - Add a polygon: `add_map_polygon([[37.78, -122.41], [37.75, -122.41], [37.75, -122.45], [37.78, -122.45]])`
        - Add a styled polygon: `add_map_polygon([[37.78, -122.41], [37.75, -122.41], [37.75, -122.45]], color="red", fill_opacity=0.3)`
    """
    if not ctx.request_context.lifespan_context.flask_server:
        return "Map server is not available."
    
    # Validate coordinates
    if not coordinates or not all(len(point) == 2 and all(isinstance(c, (int, float)) for c in point) for point in coordinates):
        return "Error: coordinates must be a list of [latitude, longitude] points."
    
    if len(coordinates) < 3:
        return "Error: a polygon requires at least 3 points."
    
    # Prepare options
    options = {}
    if color:
        options["color"] = color
    if fill_color:
        options["fillColor"] = fill_color
    if fill_opacity is not None:
        if not 0 <= fill_opacity <= 1:
            return "Error: fill_opacity must be between 0.0 and 1.0."
        options["fillOpacity"] = fill_opacity
    if weight is not None:
        if not isinstance(weight, int) or weight < 0:
            return "Error: weight must be a positive integer."
        options["weight"] = weight
    options["fitBounds"] = fit_bounds
    
    # Send the command to the map
    server = ctx.request_context.lifespan_context.flask_server
    server.show_polygon(coordinates, options)
    
    # Generate success message
    style_info = ""
    if any(key in options for key in ["color", "fillColor", "fillOpacity", "weight"]):
        style_parts = []
        if color:
            style_parts.append(f"color: {color}")
        if fill_color:
            style_parts.append(f"fill: {fill_color}")
        if fill_opacity is not None:
            style_parts.append(f"opacity: {fill_opacity}")
        if weight is not None:
            style_parts.append(f"weight: {weight}")
        style_info = f" with {', '.join(style_parts)}"
    
    bounds_info = " (map zoomed to fit)" if fit_bounds else ""
    
    return f"Polygon added with {len(coordinates)} points{style_info}{bounds_info}"

@mcp.tool()
async def add_map_line(
    ctx: Context,
    coordinates: List[List[float]],
    color: Optional[str] = None,
    weight: Optional[int] = None,
    opacity: Optional[float] = None,
    dash_array: Optional[str] = None,
    fit_bounds: bool = False
) -> str:
    """
    Add a line (polyline) to the map with the specified coordinates.

    If you're trying to add a line with more than 20 points, stop and use
    ST_Simplify to reduce the number of points.
    
    Args:
        coordinates: List of [latitude, longitude] points defining the line
        color: Line color (CSS color value)
        weight: Line width in pixels
        opacity: Line opacity (0.0 to 1.0)
        dash_array: SVG dash array pattern for creating dashed lines (e.g., "5,10")
        fit_bounds: Whether to zoom the map to show the entire line
        
    Examples:
        - Add a simple line: `add_map_line([[37.78, -122.41], [37.75, -122.41], [37.75, -122.45]])`
        - Add a styled line: `add_map_line([[37.78, -122.41], [37.75, -122.41]], color="blue", weight=3, dash_array="5,10")`
    """
    if not ctx.request_context.lifespan_context.flask_server:
        return "Map server is not available."
    
    # Validate coordinates
    if not coordinates or not all(len(point) == 2 and all(isinstance(c, (int, float)) for c in point) for point in coordinates):
        return "Error: coordinates must be a list of [latitude, longitude] points."
    
    if len(coordinates) < 2:
        return "Error: a line requires at least 2 points."
    
    # Prepare options
    options = {}
    if color:
        options["color"] = color
    if weight is not None:
        if not isinstance(weight, int) or weight < 0:
            return "Error: weight must be a positive integer."
        options["weight"] = weight
    if opacity is not None:
        if not 0 <= opacity <= 1:
            return "Error: opacity must be between 0.0 and 1.0."
        options["opacity"] = opacity
    if dash_array:
        options["dashArray"] = dash_array
    options["fitBounds"] = fit_bounds
    
    # Send the command to the map
    server = ctx.request_context.lifespan_context.flask_server
    server.show_line(coordinates, options)
    
    # Generate success message
    style_info = ""
    if any(key in options for key in ["color", "weight", "opacity", "dashArray"]):
        style_parts = []
        if color:
            style_parts.append(f"color: {color}")
        if weight is not None:
            style_parts.append(f"weight: {weight}")
        if opacity is not None:
            style_parts.append(f"opacity: {opacity}")
        if dash_array:
            style_parts.append(f"dash pattern: {dash_array}")
        style_info = f" with {', '.join(style_parts)}"
    
    bounds_info = " (map zoomed to fit)" if fit_bounds else ""
    
    return f"Line added with {len(coordinates)} points{style_info}{bounds_info}"

@mcp.tool()
async def get_map_view(ctx: Context) -> str:
    """
    Get the current map view information including center coordinates, zoom
    level, and bounds. The user can pan and zoom the map at will, at any time,
    so if you ever need to know the current view, call this tool.
    
    Returns:
        JSON string containing the current map view information
        
    Examples:
        - Get current view: `get_map_view()`
    """
    if not ctx.request_context.lifespan_context.flask_server:
        return "Map server is not available."
    
    # Get the current view from the map server
    server = ctx.request_context.lifespan_context.flask_server
    view_info = server.get_current_view()
    
    # Format the response
    response = {
        "center": view_info.get("center"),
        "zoom": view_info.get("zoom"),
        "bounds": view_info.get("bounds")
    }
    
    return json.dumps(response, indent=2)

def run_server():
    """Run the MCP server"""
    mcp.run()


if __name__ == "__main__":
    run_server() 