# MCP OpenStreetMap Server

This project provides an MCP (Machine Conversation Protocol) server for interacting with OpenStreetMap data stored in a PostgreSQL database. It also includes a Flask web server that serves a Leaflet map with OpenStreetMap tiles.

## Features

- MCP server for querying OpenStreetMap data
- Flask web server with Leaflet map integration
- OpenStreetMap tile display

## Setup

1. Install dependencies:
   ```
   uv add flask
   ```

2. Set environment variables (optional):
   ```
   export POSTGRES_HOST=localhost
   export POSTGRES_PORT=5432
   export POSTGRES_DB=osm
   export POSTGRES_USER=postgres
   export POSTGRES_PASSWORD=postgres
   export FLASK_HOST=127.0.0.1
   export FLASK_PORT=5000
   ```

3. Run the server:
   ```
   python osm_mcp_server.py
   ```

4. Access the map at http://127.0.0.1:5000

## Project Structure

- `osm_mcp_server.py`: Main MCP server implementation
- `flask_server.py`: Flask server implementation for serving the Leaflet map
- `templates/index.html`: HTML template with Leaflet map
- `static/`: Directory for static files (CSS, JS, images)

## Environment Variables

- `POSTGRES_HOST`: PostgreSQL host (default: localhost)
- `POSTGRES_PORT`: PostgreSQL port (default: 5432)
- `POSTGRES_DB`: PostgreSQL database name (default: osm)
- `POSTGRES_USER`: PostgreSQL username (default: postgres)
- `POSTGRES_PASSWORD`: PostgreSQL password (default: postgres)
- `FLASK_HOST`: Flask server host (default: 127.0.0.1)
- `FLASK_PORT`: Flask server port (default: 5000)
