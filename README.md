# MCP-OSM: OpenStreetMap Integration for MCP

This package provides OpenStreetMap integration for MCP, allowing users to visualize map data and interact with it through an MCP interface.

## Features

- Web-based map viewer using Leaflet.js and OpenStreetMap
- Server-to-client communication via Server-Sent Events (SSE)
- MCP tools for map control (adding markers, polygons, setting view)
- PostgreSQL/PostGIS query interface for OpenStreetMap data

## Installation

```bash
pip install -e .
```

## Usage

### Running the Server

You can run the server using the provided run script:

```bash
./run.py
```

Or as a Python module:

```bash
python -m mcp_osm
```

This will start both the MCP server and a Flask web server. The web interface will be available at http://127.0.0.1:5000 by default.

### Environment Variables

The following environment variables can be used to configure the servers:

- `FLASK_HOST` - Host for the Flask server (default: 127.0.0.1)
- `FLASK_PORT` - Port for the Flask server (default: 5000)
- `PGHOST` - PostgreSQL host (default: localhost)
- `PGPORT` - PostgreSQL port (default: 5432)
- `PGDB` - PostgreSQL database name (default: osm)
- `PGUSER` - PostgreSQL username (default: postgres)
- `PGPASSWORD` - PostgreSQL password (default: postgres)

### MCP Tools

The following MCP tools are available:

- `set_map_view` - Set the map view to specific coordinates or bounds
- `set_map_title` - Set the title displayed at the bottom right of the map
- `add_map_marker` - Add a marker at specific coordinates
- `add_map_polygon` - Add a polygon defined by a set of coordinates
- `query_osm_postgres` - Execute a SQL query against the OpenStreetMap database

## Project Structure

```
mcp-osm/
├── mcp_osm/
│   ├── __init__.py
│   ├── __main__.py
│   ├── flask_server.py
│   └── server.py
├── templates/
│   └── index.html
├── static/
│   └── ...
├── setup.py
├── run.py
└── README.md
```

## Development

To set up the development environment:

1. Clone the repository
2. Install the package in development mode: `pip install -e .`
3. Make your changes
4. Run the server: `./run.py`

## License

[MIT License](LICENSE)
