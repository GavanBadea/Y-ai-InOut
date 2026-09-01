"""
تشغيل الخادم على كومبيوتر 1 — يستقبل اتصالات المتصفح من أجهزة الشبكة (WiFi/LAN).
"""
import os
import socket
import sys

from app import NETWORK, app, ensure_schema


def detect_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return '127.0.0.1'


def print_banner(host, port, lan_ip):
    url_lan = f'http://{lan_ip}:{port}'
    url_local = f'http://127.0.0.1:{port}'
    line = '=' * 56
    print(line)
    print('  Y-ai InOut — وضع الشبكة (خادم)')
    print(line)
    print(f'  على هذا الجهاز:     {url_local}')
    print(f'  من أجهزة WiFi:      {url_lan}')
    print()
    print('  ضع هذا الرابط في network_config.json كـ server_url')
    print(f'  ثم انسخ الملف لأجهزة الأقسام أو شغّل Y-inout-client.bat')
    print(line)


def run_waitress(host, port, threads):
    from waitress import serve
    serve(app, host=host, port=port, threads=threads, channel_timeout=120)


def run_flask(host, port):
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


def main():
    ensure_schema()
    host = NETWORK.get('host', '0.0.0.0')
    port = int(NETWORK.get('port', 8000))
    threads = int(NETWORK.get('threads', 12))
    lan_ip = detect_lan_ip()
    print_banner(host, port, lan_ip)

    try:
        run_waitress(host, port, threads)
    except ImportError:
        print('تلميح: ثبّت waitress لأداء أفضل مع 20+ جهاز: pip install waitress')
        run_flask(host, port)
    except KeyboardInterrupt:
        print('\nتم إيقاف الخادم.')
        sys.exit(0)


if __name__ == '__main__':
    main()
