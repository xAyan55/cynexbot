#!/bin/bash
set -e

# CynexBot 1-Click Installer
# Installs python, git, sets up a virtual environment, configures systemd, and starts the bot.

INSTALL_DIR="/opt/cynexbot"
REPO_URL="https://github.com/xAyan55/cynexbot.git"

echo "=========================================="
echo "          CynexBot Installer              "
echo "=========================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root or with sudo:"
  echo "sudo bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/xAyan55/cynexbot/main/install.sh)\""
  exit 1
fi

# 1. Install prerequisites
echo "[1/5] Installing system prerequisites (git, python3, venv)..."
apt-get update
apt-get install -y git python3 python3-pip python3-venv

# 2. Clone the repository
if [ -d "$INSTALL_DIR" ]; then
  echo "[2/5] Directory $INSTALL_DIR already exists. Updating code instead of cloning..."
  cd "$INSTALL_DIR"
  git pull
else
  echo "[2/5] Cloning repository to $INSTALL_DIR..."
  git clone "$REPO_URL" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi

# 3. Create virtual environment and install dependencies
echo "[3/5] Setting up Python virtual environment & dependencies..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 4. Prompt for Discord Token
echo "[4/5] Configuring bot token..."
if [ ! -f "token.txt" ]; then
  read -p "Enter your Discord Bot Token (leave empty to configure later): " DISCORD_TOKEN
  if [ -n "$DISCORD_TOKEN" ]; then
    echo "$DISCORD_TOKEN" > token.txt
    chmod 600 token.txt
    echo "Token successfully saved to token.txt."
  else
    echo "No token provided. You must write your token to $INSTALL_DIR/token.txt before running the bot."
  fi
else
  echo "Existing token.txt found. Skipping token prompt."
fi

# 5. Configure systemd service
echo "[5/5] Configuring systemd service..."
if [ -f "breeze.service" ]; then
  cp breeze.service /etc/systemd/system/breeze.service
  
  # Dynamically replace WorkingDirectory and ExecStart to match the installation path
  sed -i "s|WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" /etc/systemd/system/breeze.service
  sed -i "s|ExecStart=.*|ExecStart=$INSTALL_DIR/venv/bin/python bot.py|" /etc/systemd/system/breeze.service
  
  systemctl daemon-reload
  systemctl enable breeze.service
  
  # Start or restart bot if token is present
  if [ -f "token.txt" ] && [ -s "token.txt" ]; then
    echo "Starting CynexBot service..."
    systemctl restart breeze.service
    echo "Checking status..."
    systemctl status breeze.service --no-pager -n 5
    echo "=========================================="
    echo " CynexBot installed and started successfully!"
    echo "=========================================="
  else
    echo "=========================================="
    echo " Installation complete!"
    echo " Please put your token in $INSTALL_DIR/token.txt"
    echo " Then start the bot with: systemctl start breeze"
    echo "=========================================="
  fi
else
  echo "Error: breeze.service not found in repository. Service setup failed."
  exit 1
fi
