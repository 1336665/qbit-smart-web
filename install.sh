#!/bin/bash

#===============================================================================
#
#   qBit Smart Web Manager - 一键安装/更新脚本
#   
#   功能：
#   - 自动安装所有依赖
#   - 支持绑定域名 + HTTPS (Let's Encrypt)
#   - 支持一键更新
#   - systemd 服务管理
#
#   使用方法：
#   bash <(curl -sL https://raw.githubusercontent.com/1336665/Qbit-Smart-Web/main/install.sh)
#
#===============================================================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# 配置
REPO_URL="https://github.com/1336665/Qbit-Smart-Web"
REPO_RAW_URL="https://raw.githubusercontent.com/1336665/Qbit-Smart-Web/main"
INSTALL_DIR="/opt/qbit-smart-web"
SERVICE_NAME="qbit-smart"
DEFAULT_PORT=5000
VERSION="1.16.0"

# 打印带颜色的消息
print_msg() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# 显示Logo
show_logo() {
    clear
    echo -e "${CYAN}"
    cat << 'EOF'
╔═══════════════════════════════════════════════════════════════════════════╗
║                                                                           ║
║            ██████╗ ██████╗ ██╗████████╗    ███████╗███╗   ███╗            ║
║           ██╔═══██╗██╔══██╗██║╚══██╔══╝    ██╔════╝████╗ ████║            ║
║           ██║   ██║██████╔╝██║   ██║       ███████╗██╔████╔██║            ║
║           ██║▄▄ ██║██╔══██╗██║   ██║       ╚════██║██║╚██╔╝██║            ║
║           ╚██████╔╝██████╔╝██║   ██║       ███████║██║ ╚═╝ ██║            ║
║            ╚══▀▀═╝ ╚═════╝ ╚═╝   ╚═╝       ╚══════╝╚═╝     ╚═╝            ║
║                                                                           ║
║                    qBit Smart Web Manager v1.16                           ║
║                         一键安装/管理脚本                                  ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"
}

# 检测系统
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
        VERSION_ID=$VERSION_ID
    elif [ -f /etc/redhat-release ]; then
        OS="centos"
    else
        OS=$(uname -s)
    fi
    
    case "$OS" in
        ubuntu|debian)
            PKG_MANAGER="apt"
            PKG_UPDATE="apt update"
            PKG_INSTALL="apt install -y"
            ;;
        centos|rhel|fedora|rocky|almalinux)
            PKG_MANAGER="yum"
            PKG_UPDATE="yum makecache"
            PKG_INSTALL="yum install -y"
            if command -v dnf &> /dev/null; then
                PKG_MANAGER="dnf"
                PKG_UPDATE="dnf makecache"
                PKG_INSTALL="dnf install -y"
            fi
            ;;
        *)
            print_error "不支持的操作系统: $OS"
            exit 1
            ;;
    esac
    
    print_msg "检测到系统: $OS"
}

# 检查root权限
check_root() {
    if [ "$EUID" -ne 0 ]; then
        print_error "请使用 root 用户运行此脚本"
        exit 1
    fi
}

# 安装基础依赖
install_dependencies() {
    print_msg "正在安装系统依赖..."
    
    $PKG_UPDATE
    
    if [ "$PKG_MANAGER" = "apt" ]; then
        $PKG_INSTALL python3 python3-pip python3-venv curl wget unzip git nginx certbot python3-certbot-nginx
    else
        $PKG_INSTALL python3 python3-pip curl wget unzip git nginx certbot python3-certbot-nginx
    fi
    
    print_msg "正在安装 Python 依赖..."
    
    # 尝试使用 --break-system-packages（新版本Python需要）
    pip3 install --break-system-packages flask requests beautifulsoup4 lxml feedparser qbittorrent-api 2>/dev/null || \
    pip3 install flask requests beautifulsoup4 lxml feedparser qbittorrent-api
    
    print_success "依赖安装完成"
}

# 下载/更新程序
download_program() {
    print_msg "正在下载 qBit Smart Web Manager..."
    
    # 备份数据库（如果存在）
    if [ -f "$INSTALL_DIR/qbit_smart.db" ]; then
        print_msg "备份现有数据库..."
        cp "$INSTALL_DIR/qbit_smart.db" "/tmp/qbit_smart.db.backup"
    fi
    
    # 创建安装目录
    mkdir -p "$INSTALL_DIR"
    
    # 下载最新版本
    cd /tmp
    rm -rf qbit-smart-web-latest.zip qbit-smart-web-*
    
    wget -q "${REPO_URL}/releases/latest/download/qbit-smart-web.zip" -O qbit-smart-web-latest.zip || {
        # 如果没有release，从main分支下载
        wget -q "${REPO_URL}/archive/refs/heads/main.zip" -O qbit-smart-web-latest.zip
    }
    
    rm -rf /tmp/qbit-smart-web-extract
    unzip -q qbit-smart-web-latest.zip -d /tmp/qbit-smart-web-extract
    
    # 找到解压后的目录
    EXTRACTED_DIR=$(find /tmp/qbit-smart-web-extract -mindepth 1 -maxdepth 1 -type d | head -1)
    if [ -z "$EXTRACTED_DIR" ]; then
        EXTRACTED_DIR="/tmp/qbit-smart-web-extract"
    fi
    
    if [ -z "$EXTRACTED_DIR" ]; then
        print_error "解压失败，找不到程序目录"
        exit 1
    fi
    
    # 复制文件
    cp -r "$EXTRACTED_DIR"/* "$INSTALL_DIR/"
    
    # 恢复数据库
    if [ -f "/tmp/qbit_smart.db.backup" ]; then
        print_msg "恢复数据库..."
        cp "/tmp/qbit_smart.db.backup" "$INSTALL_DIR/qbit_smart.db"
        rm -f "/tmp/qbit_smart.db.backup"
    fi
    
    # 清理
    rm -rf /tmp/qbit-smart-web-*
    
    print_success "程序下载完成"
}

# 配置systemd服务
setup_service() {
    local port=$1
    
    print_msg "配置 systemd 服务..."
    
    cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=qBit Smart Web Manager
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment="PORT=${port}"
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/app.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable ${SERVICE_NAME}
    
    print_success "服务配置完成"
}

# 配置Nginx反向代理
setup_nginx() {
    local domain=$1
    local port=$2
    
    print_msg "配置 Nginx 反向代理..."
    
    cat > /etc/nginx/sites-available/qbit-smart << EOF
server {
    listen 80;
    server_name ${domain};
    
    location / {
        proxy_pass http://127.0.0.1:${port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
        proxy_read_timeout 86400;
    }
}
EOF

    # 启用站点
    ln -sf /etc/nginx/sites-available/qbit-smart /etc/nginx/sites-enabled/
    
    # 删除默认站点（如果存在）
    rm -f /etc/nginx/sites-enabled/default
    
    # 测试配置
    nginx -t
    
    # 重启Nginx
    systemctl restart nginx
    systemctl enable nginx
    
    print_success "Nginx 配置完成"
}

# 申请SSL证书
setup_ssl() {
    local domain=$1
    local email=$2
    
    print_msg "正在申请 Let's Encrypt SSL 证书..."
    
    certbot --nginx -d "$domain" --non-interactive --agree-tos --email "$email" --redirect
    
    # 设置自动续期
    systemctl enable certbot.timer
    systemctl start certbot.timer
    
    print_success "SSL 证书配置完成"
}

# 启动服务
start_service() {
    print_msg "启动服务..."
    systemctl restart ${SERVICE_NAME}
    sleep 2
    
    if systemctl is-active --quiet ${SERVICE_NAME}; then
        print_success "服务启动成功"
    else
        print_error "服务启动失败，请检查日志: journalctl -u ${SERVICE_NAME} -f"
        exit 1
    fi
}

# 停止服务
stop_service() {
    print_msg "停止服务..."
    systemctl stop ${SERVICE_NAME} 2>/dev/null || true
}

# 显示状态
show_status() {
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  qBit Smart Web Manager 安装完成！${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    
    if [ -n "$DOMAIN" ]; then
        if [ "$USE_SSL" = "y" ]; then
            echo -e "  访问地址: ${GREEN}https://${DOMAIN}${NC}"
        else
            echo -e "  访问地址: ${GREEN}http://${DOMAIN}${NC}"
        fi
    else
        echo -e "  访问地址: ${GREEN}http://YOUR_SERVER_IP:${PORT}${NC}"
    fi
    
    echo ""
    echo -e "  ${YELLOW}管理命令:${NC}"
    echo -e "    启动: ${CYAN}systemctl start ${SERVICE_NAME}${NC}"
    echo -e "    停止: ${CYAN}systemctl stop ${SERVICE_NAME}${NC}"
    echo -e "    重启: ${CYAN}systemctl restart ${SERVICE_NAME}${NC}"
    echo -e "    状态: ${CYAN}systemctl status ${SERVICE_NAME}${NC}"
    echo -e "    日志: ${CYAN}journalctl -u ${SERVICE_NAME} -f${NC}"
    echo ""
    echo -e "  ${YELLOW}更新程序:${NC}"
    echo -e "    ${CYAN}bash <(curl -sL ${REPO_RAW_URL}/install.sh)${NC}"
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
}

# 卸载
uninstall() {
    print_warn "即将卸载 qBit Smart Web Manager"
    read -p "是否确认卸载？(y/n): " confirm
    
    if [ "$confirm" != "y" ]; then
        print_msg "取消卸载"
        return
    fi
    
    print_msg "停止服务..."
    systemctl stop ${SERVICE_NAME} 2>/dev/null || true
    systemctl disable ${SERVICE_NAME} 2>/dev/null || true
    
    print_msg "删除服务文件..."
    rm -f /etc/systemd/system/${SERVICE_NAME}.service
    systemctl daemon-reload
    
    print_msg "删除Nginx配置..."
    rm -f /etc/nginx/sites-available/qbit-smart
    rm -f /etc/nginx/sites-enabled/qbit-smart
    systemctl restart nginx 2>/dev/null || true
    
    read -p "是否删除程序文件和数据库？(y/n): " del_data
    if [ "$del_data" = "y" ]; then
        rm -rf "$INSTALL_DIR"
        print_msg "程序文件已删除"
    fi
    
    print_success "卸载完成"
}

# 主菜单
main_menu() {
    show_logo
    
    echo -e "${YELLOW}请选择操作:${NC}"
    echo ""
    echo -e "  ${GREEN}1)${NC} 全新安装"
    echo -e "  ${GREEN}2)${NC} 更新程序"
    echo -e "  ${GREEN}3)${NC} 配置域名/HTTPS"
    echo -e "  ${GREEN}4)${NC} 查看状态"
    echo -e "  ${GREEN}5)${NC} 重启服务"
    echo -e "  ${GREEN}6)${NC} 查看日志"
    echo -e "  ${GREEN}7)${NC} 卸载"
    echo -e "  ${GREEN}0)${NC} 退出"
    echo ""
    read -p "请输入选项 [0-7]: " choice
    
    case $choice in
        1) fresh_install ;;
        2) update_install ;;
        3) configure_domain ;;
        4) systemctl status ${SERVICE_NAME} --no-pager || true; read -p "按回车键继续..." ;;
        5) systemctl restart ${SERVICE_NAME}; print_success "服务已重启"; read -p "按回车键继续..." ;;
        6) journalctl -u ${SERVICE_NAME} -n 50 --no-pager; read -p "按回车键继续..." ;;
        7) uninstall ;;
        0) exit 0 ;;
        *) print_error "无效选项"; sleep 1 ;;
    esac
    
    main_menu
}

# 全新安装
fresh_install() {
    show_logo
    
    print_msg "开始全新安装..."
    echo ""
    
    # 端口配置
    read -p "请输入监听端口 [默认: ${DEFAULT_PORT}]: " PORT
    PORT=${PORT:-$DEFAULT_PORT}
    
    # 域名配置
    echo ""
    read -p "是否绑定域名？(y/n) [默认: n]: " USE_DOMAIN
    USE_DOMAIN=${USE_DOMAIN:-n}
    
    if [ "$USE_DOMAIN" = "y" ]; then
        read -p "请输入域名 (例如: pt.example.com): " DOMAIN
        
        if [ -z "$DOMAIN" ]; then
            print_error "域名不能为空"
            return
        fi
        
        read -p "是否配置 HTTPS (Let's Encrypt)？(y/n) [默认: y]: " USE_SSL
        USE_SSL=${USE_SSL:-y}
        
        if [ "$USE_SSL" = "y" ]; then
            read -p "请输入邮箱 (用于 Let's Encrypt): " EMAIL
            if [ -z "$EMAIL" ]; then
                print_error "邮箱不能为空"
                return
            fi
        fi
    fi
    
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "  安装配置确认:"
    echo -e "  端口: ${GREEN}${PORT}${NC}"
    if [ "$USE_DOMAIN" = "y" ]; then
        echo -e "  域名: ${GREEN}${DOMAIN}${NC}"
        echo -e "  HTTPS: ${GREEN}${USE_SSL}${NC}"
    fi
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    read -p "确认开始安装？(y/n): " confirm
    
    if [ "$confirm" != "y" ]; then
        print_msg "取消安装"
        return
    fi
    
    # 开始安装
    install_dependencies
    download_program
    setup_service "$PORT"
    
    if [ "$USE_DOMAIN" = "y" ]; then
        setup_nginx "$DOMAIN" "$PORT"
        
        if [ "$USE_SSL" = "y" ]; then
            setup_ssl "$DOMAIN" "$EMAIL"
        fi
    fi
    
    start_service
    show_status
    
    read -p "按回车键继续..."
}

# 更新安装
update_install() {
    show_logo
    
    print_msg "开始更新..."
    
    if [ ! -d "$INSTALL_DIR" ]; then
        print_error "未检测到已安装的程序，请先进行全新安装"
        read -p "按回车键继续..."
        return
    fi
    
    stop_service
    download_program
    start_service
    
    print_success "更新完成！"
    read -p "按回车键继续..."
}

# 配置域名
configure_domain() {
    show_logo
    
    if [ ! -d "$INSTALL_DIR" ]; then
        print_error "未检测到已安装的程序，请先进行安装"
        read -p "按回车键继续..."
        return
    fi
    
    read -p "请输入域名 (例如: pt.example.com): " DOMAIN
    
    if [ -z "$DOMAIN" ]; then
        print_error "域名不能为空"
        return
    fi
    
    # 获取当前端口
    PORT=$(grep -oP 'PORT=\K\d+' /etc/systemd/system/${SERVICE_NAME}.service 2>/dev/null || echo "$DEFAULT_PORT")
    
    read -p "是否配置 HTTPS？(y/n) [默认: y]: " USE_SSL
    USE_SSL=${USE_SSL:-y}
    
    if [ "$USE_SSL" = "y" ]; then
        read -p "请输入邮箱 (用于 Let's Encrypt): " EMAIL
        if [ -z "$EMAIL" ]; then
            print_error "邮箱不能为空"
            return
        fi
    fi
    
    setup_nginx "$DOMAIN" "$PORT"
    
    if [ "$USE_SSL" = "y" ]; then
        setup_ssl "$DOMAIN" "$EMAIL"
    fi
    
    print_success "域名配置完成！"
    
    if [ "$USE_SSL" = "y" ]; then
        echo -e "访问地址: ${GREEN}https://${DOMAIN}${NC}"
    else
        echo -e "访问地址: ${GREEN}http://${DOMAIN}${NC}"
    fi
    
    read -p "按回车键继续..."
}

# 主程序入口
main() {
    check_root
    detect_os
    
    # 检查是否有参数
    case "${1:-}" in
        install)
            fresh_install
            ;;
        update)
            update_install
            ;;
        uninstall)
            uninstall
            ;;
        status)
            systemctl status ${SERVICE_NAME}
            ;;
        *)
            main_menu
            ;;
    esac
}

# 运行
main "$@"
