#!/bin/bash
# 보드가 Wi-Fi 핫스팟을 열어 현장에서 휴대폰이 바로 접속하게 함 (NetworkManager 필요)
# 사용: sudo bash deploy/hotspot.sh
# 끝나면 휴대폰을 ONMAEUM Wi-Fi 에 연결하고, 앱 '주소 바꾸기'에 아래에 나오는 주소를 넣기
set -e
IFACE=${IFACE:-wlan0}            # Wi-Fi 장치 이름 (nmcli dev 로 확인)
SSID=${SSID:-ONMAEUM}            # Wi-Fi 이름
PASS=${PASS:-onmaeum1234}        # 비밀번호 (8자 이상)
nmcli dev wifi hotspot ifname "$IFACE" ssid "$SSID" password "$PASS"
nmcli connection modify Hotspot connection.autoconnect yes   # 재부팅해도 자동으로 다시 열림
IP=$(ip -4 addr show "$IFACE" | awk '/inet /{print $2}' | cut -d/ -f1)
echo "핫스팟: $SSID / $PASS"
echo "앱에 넣을 주소: http://$IP:8080"
