#!/usr/bin/env bash
# 生成 vehicle-agent-v1-readonly 本地隔离验收用 TEST-ONLY TLS 证书。
# 仅用于测试（services/protocol-tester/tests/certs/），严禁用于任何生产环境。
# 仓库 .gitignore 排除 *.key/*.crt：证书由测试会话开始时现场生成（pytest fixture
# 调用本脚本），仓库不提交任何密钥/证书文件；本脚本本身可审计重建。
set -euo pipefail

cd "$(dirname "$0")/../services/protocol-tester/tests/certs"

CN_CA="Firebot Readonly Agent Test CA (TEST ONLY)"
CN_OTHER_CA="Firebot Readonly Agent OTHER CA (TEST ONLY)"

openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 3650 \
  -subj "/CN=${CN_CA}"

openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr \
  -subj "/CN=localhost"
printf "subjectAltName=DNS:localhost,IP:127.0.0.1\n" > san.cnf
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days 3650 -extfile san.cnf

# 第二个 CA 与服务器证书：用于"错误 CA 必须被拒绝"的反证测试。
openssl req -x509 -newkey rsa:2048 -nodes -keyout other_ca.key -out other_ca.crt -days 3650 \
  -subj "/CN=${CN_OTHER_CA}"
openssl req -newkey rsa:2048 -nodes -keyout other_server.key -out other_server.csr \
  -subj "/CN=localhost"
openssl x509 -req -in other_server.csr -CA other_ca.crt -CAkey other_ca.key -CAcreateserial \
  -out other_server.crt -days 3650 -extfile san.cnf

rm -f server.csr other_server.csr san.cnf ca.srl other_ca.srl
echo "TEST-ONLY certs regenerated under services/protocol-tester/tests/certs/"
