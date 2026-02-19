# Use a lightweight Python image
FROM python:3.11-slim

# Set environment variables to optimize Python for Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies required for some Python packages (like PyNaCl for voice)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies directly 
# (Based on requirements specified in SETUP.md)
RUN pip install --no-cache-dir \
    discord.py>=2.0.0 \
    python-dotenv>=0.19.2 \
    aiohttp>=3.8.1 \
    pynacl>=1.5.0

# Copy the source code from the DebugScriptHelper directory to /app
COPY DebugScriptHelper/ .

# Ensure the data file exists so Docker can handle the volume mount correctly
RUN touch event_data.pkl

# Start the bot
CMD ["python", "bot.py"]
