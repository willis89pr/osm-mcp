#!/usr/bin/env python3
from setuptools import setup, find_packages

setup(
    name="mcp-osm",
    version="0.1.0",
    description="MCP server with OpenStreetMap integration",
    author="Your Name",
    author_email="your.email@example.com",
    # Explicitly define packages to include
    packages=find_packages(exclude=["static", "templates"]),
    # Include our templates and static files as package data
    package_data={
        "": ["templates/*", "static/*", "static/*/*"],
    },
    include_package_data=True,
    # Define dependencies
    install_requires=[
        "flask>=3.1.0",
        "psycopg2>=2.9.10",
        "fastmcp",
    ],
    python_requires=">=3.7",
) 