# Use an official Python image
FROM python:3.11-slim

# Install Chrome & dependencies
RUN apt-get update \
 && apt-get install -y wget gnupg unzip \
 # Add Google’s signing key & repo
 && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
      | apt-key add - \
 && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update \
 && apt-get install -y google-chrome-stable \
 # Install chromedriver matching Chrome version
 && CHROME_VER=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+') \
 && wget -O /tmp/chromedriver.zip \
      "https://chromedriver.storage.googleapis.com/${CHROME_VER}/chromedriver_linux64.zip" \
 && unzip /tmp/chromedriver.zip -d /usr/local/bin/ \
 && rm /tmp/chromedriver.zip \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -r requirements.txt

# Healthcheck ensures the process is running
HEALTHCHECK --interval=30s CMD pgrep -f main.py || exit 1

# Render will invoke this
CMD ["python", "main.py"]
