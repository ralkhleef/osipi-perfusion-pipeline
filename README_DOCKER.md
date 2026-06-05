# OSIPI Pipeline Local App Setup

This runs the OSIPI Pipeline web app on your own computer.

You do not need to install Python. You do not need to code. There is no hosting cost.

Open the app here:

http://localhost:8000

Project source:

[GitHub Repository](https://github.com/ralkhleef/osipi-perfusion-pipeline)

## Requirements

- Docker Desktop installed
- Docker Desktop running

## Mac

1. Install Docker Desktop.
2. Open Docker Desktop once and wait until it is running.
3. Unzip the OSIPI app folder.
4. Double-click `start.command`.
5. If the browser does not open automatically, open http://localhost:8000.

To stop the app, double-click `stop.sh`, or run:

```bash
./stop.sh
```

## Windows

1. Install Docker Desktop.
2. Open Docker Desktop once and wait until it is running.
3. Unzip the OSIPI app folder.
4. Double-click `start.bat`.
5. If the browser does not open automatically, open http://localhost:8000.

To stop the app, double-click `stop.bat`.

## Troubleshooting

### Docker is not installed

Install Docker Desktop first:

https://www.docker.com/products/docker-desktop/

Then run the start script again.

### Docker is installed but not open

Open Docker Desktop and wait until it finishes starting.

On Mac, `start.command` will try to open Docker Desktop for you.

### Port 8000 is already being used

Another app is already using `localhost:8000`.

Stop the other app, then run the start script again.

### App does not open automatically

Open your browser and go to http://localhost:8000.

## What Gets Saved

Uploaded submissions and validation outputs are saved locally in:

- `submissions/`
- `data/`

These folders are kept even if the Docker container restarts.

## Script Locations

The easy launch files stay at the project root. The organized script files live in:

- `scripts/start/`
- `scripts/stop/`
- `scripts/release/`
