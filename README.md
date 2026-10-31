# Backend

This is the Python 3.11 FastAPI and MCP backend migrated from the working system. It keeps the original API prefix (`/api/v1/mcp`) and database model. The MySQL schema is initialized by the existing backend startup routine.

## Local setup

1. Create a Python 3.11 environment and install dependencies:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\python -m pip install -r requirements.txt
   ```

2. Copy `config.example.json` to `config.json`. Set the MySQL connection, a private JWT signing key, and a private default user password before starting the service.

3. Start the combined FastAPI and MCP service from this directory:

   ```powershell
   .\.venv\Scripts\python run.py
   ```

The API listens on `http://127.0.0.1:8002`; OpenAPI is available at `/docs`. Runtime uploads, parsed answer packages, logs, local databases, and private configuration remain under ignored paths.

## Local answer package storage

Parsed packages and their image/answer files are stored under
`data/teacher_scoring/local-packages` relative to this backend directory. The
database stores paths relative to the backend root, so this directory must be
copied or mounted into the same relative location when moving the database and
deploying the backend. The directory is intentionally excluded from Git because
it contains uploaded student files; it must be migrated as deployment data and
persisted independently from the code. The running application reads package
files only from its own backend root.
