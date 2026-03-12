#!/bin/bash
# Bambuddy Middleware - Raspberry Pi Installation Script
#
# Usage:
#   chmod +x install.sh
#   sudo ./install.sh
#
# This script:
#   1. Installs Python dependencies
#   2. Copies files to /opt/bambuddy-middleware/
#   3. Creates default config at /etc/bambuddy-middleware/config.json
#   4. Installs systemd service
#   5. Enables and starts the service

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Bambuddy Middleware Installer${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Check root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: Please run as root (sudo ./install.sh)${NC}"
    exit 1
fi

# Check Python 3.11+
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]); then
    echo -e "${RED}Error: Python 3.11+ is required (found $PYTHON_VERSION)${NC}"
    echo "Install with: sudo apt install python3.11"
    exit 1
fi

echo -e "${GREEN}✓ Python $PYTHON_VERSION${NC}"

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Installation paths
INSTALL_DIR="/opt/bambuddy-middleware"
CONFIG_DIR="/etc/bambuddy-middleware"
DATA_DIR="/var/lib/bambuddy-middleware"

# Step 1: Install Python dependencies
echo ""
echo -e "${YELLOW}Installing Python dependencies...${NC}"
pip3 install --quiet pyserial cryptography
echo -e "${GREEN}✓ Dependencies installed${NC}"

# Step 2: Copy files
echo ""
echo -e "${YELLOW}Installing middleware to ${INSTALL_DIR}...${NC}"
mkdir -p "$INSTALL_DIR"

# Copy all Python files
for f in bambuddy_middleware.py serial_connection.py gcode_middleware.py \
         mqtt_server.py ftp_server.py ssdp_server.py bind_server.py \
         certificate.py requirements.txt; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
    fi
done

chmod +x "$INSTALL_DIR/bambuddy_middleware.py"
echo -e "${GREEN}✓ Files installed${NC}"

# Step 3: Create default config
echo ""
echo -e "${YELLOW}Creating configuration...${NC}"
mkdir -p "$CONFIG_DIR"
mkdir -p "$DATA_DIR"
mkdir -p "$DATA_DIR/uploads/cache"
mkdir -p "$DATA_DIR/certs"

if [ ! -f "$CONFIG_DIR/config.json" ]; then
    cat > "$CONFIG_DIR/config.json" << 'EOF'
{
    "access_code": "12345678",
    "printer_name": "Bambuddy Middleware",
    "serial": "00M09A391800001",
    "model": "3DPrinter-X1-Carbon",
    "serial_port": "/dev/ttyUSB0",
    "baudrate": 115200,
    "data_dir": "/var/lib/bambuddy-middleware",
    "log_level": "INFO"
}
EOF
    echo -e "${GREEN}✓ Default config created at ${CONFIG_DIR}/config.json${NC}"
    echo -e "${YELLOW}  ⚠ Edit this file to set your access code and serial port!${NC}"
else
    echo -e "${GREEN}✓ Config already exists, keeping current settings${NC}"
fi

# Step 4: Install systemd service
echo ""
echo -e "${YELLOW}Installing systemd service...${NC}"
cp "$SCRIPT_DIR/bambuddy-middleware.service" /etc/systemd/system/
systemctl daemon-reload
echo -e "${GREEN}✓ Service installed${NC}"

# Step 5: Detect serial ports
echo ""
echo -e "${YELLOW}Detecting serial ports...${NC}"
PORTS=$(python3 -c "
from serial.tools.list_ports import comports
for p in comports():
    print(f'  {p.device:20s}  {p.description}')
" 2>/dev/null || echo "  (none detected)")

echo "Available serial ports:"
echo "$PORTS"
echo ""

# Step 6: Enable and start
echo -e "${YELLOW}Enabling service...${NC}"
systemctl enable bambuddy-middleware
echo -e "${GREEN}✓ Service enabled (starts on boot)${NC}"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Next steps:"
echo ""
echo -e "  1. ${YELLOW}Edit configuration:${NC}"
echo "     sudo nano $CONFIG_DIR/config.json"
echo ""
echo -e "  2. ${YELLOW}Set your serial port and access code:${NC}"
echo "     - serial_port: Check 'Available serial ports' above"
echo "     - access_code: 8 characters, used in slicer"
echo ""
echo -e "  3. ${YELLOW}Start the service:${NC}"
echo "     sudo systemctl start bambuddy-middleware"
echo ""
echo -e "  4. ${YELLOW}Check status:${NC}"
echo "     sudo systemctl status bambuddy-middleware"
echo "     sudo journalctl -u bambuddy-middleware -f"
echo ""
echo -e "  5. ${YELLOW}Add printer in Bambu Studio/OrcaSlicer:${NC}"
echo "     The printer should appear automatically in the slicer's"
echo "     network printer discovery."
echo ""
