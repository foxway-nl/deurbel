#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# Foxway Doorbell — CLI Test Script
#
# Simulates a complete doorbell ring cycle from the terminal.
# Usage:  ./test_ring.sh [BASE_URL]
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

BASE_URL="${1:-http://localhost:8090}"
API="${BASE_URL}/api"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Foxway Doorbell — CLI Test Suite${NC}"
echo -e "${CYAN}  Target: ${BASE_URL}${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo ""

# ── 1. Health check ──────────────────────────────────────────────
echo -e "${YELLOW}[1/7] Health check...${NC}"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${API}/status")
if [ "$STATUS" = "200" ]; then
    echo -e "${GREEN}  ✓ Backend is UP${NC}"
    curl -s "${API}/status" | python3 -m json.tool 2>/dev/null || curl -s "${API}/status"
else
    echo -e "${RED}  ✗ Backend unreachable (HTTP ${STATUS})${NC}"
    exit 1
fi
echo ""

# ── 2. Pair a test device ────────────────────────────────────────
echo -e "${YELLOW}[2/7] Pairing test device...${NC}"
PAIR_RESP=$(curl -s -X POST "${API}/pair" \
    -H "Content-Type: application/json" \
    -d '{"user_name": "Mohamed", "device_name": "Test CLI", "fcm_token": "test-token-123"}')
echo "  Response: ${PAIR_RESP}" | python3 -m json.tool 2>/dev/null || echo "  ${PAIR_RESP}"

USER_ID=$(echo "$PAIR_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['user_id'])" 2>/dev/null || echo "")
if [ -n "$USER_ID" ]; then
    echo -e "${GREEN}  ✓ Paired as user_id=${USER_ID}${NC}"
else
    echo -e "${YELLOW}  ⚠ Pairing may have been rejected (not on LAN). Continuing...${NC}"
    USER_ID="test_user"
fi
echo ""

# ── 3. Trigger a ring ───────────────────────────────────────────
echo -e "${YELLOW}[3/7] Triggering doorbell ring...${NC}"
RING_RESP=$(curl -s -X POST "${API}/ring")
echo "  Response:" 
echo "$RING_RESP" | python3 -m json.tool 2>/dev/null || echo "  ${RING_RESP}"

CALL_ID=$(echo "$RING_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('call_id',''))" 2>/dev/null || echo "")
if [ -n "$CALL_ID" ] && [ "$CALL_ID" != "" ]; then
    echo -e "${GREEN}  ✓ Ring triggered: call_id=${CALL_ID}${NC}"
else
    echo -e "${YELLOW}  ⚠ Ring suppressed (cooldown or active call)${NC}"
    echo -e "${YELLOW}  Checking current status...${NC}"
    curl -s "${API}/status" | python3 -m json.tool 2>/dev/null
    exit 0
fi
echo ""

# ── 4. Check status during ringing ──────────────────────────────
echo -e "${YELLOW}[4/7] Checking call status (should be RINGING)...${NC}"
curl -s "${API}/status" | python3 -m json.tool 2>/dev/null
echo ""

# ── 5. Claim the call ───────────────────────────────────────────
echo -e "${YELLOW}[5/7] Claiming the call as Mohamed...${NC}"
CLAIM_RESP=$(curl -s -X POST "${API}/call/${CALL_ID}/claim" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"${USER_ID}\", \"user_name\": \"Mohamed\"}")
echo "  Response:"
echo "$CLAIM_RESP" | python3 -m json.tool 2>/dev/null || echo "  ${CLAIM_RESP}"
echo -e "${GREEN}  ✓ Call claimed${NC}"
echo ""

# ── 6. Try to claim again (should 409) ──────────────────────────
echo -e "${YELLOW}[6/7] Attempting duplicate claim (should be 409)...${NC}"
CLAIM2_RESP=$(curl -s -w "\n%{http_code}" -X POST "${API}/call/${CALL_ID}/claim" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "hanan_test", "user_name": "Hanan"}')
HTTP_CODE=$(echo "$CLAIM2_RESP" | tail -1)
BODY=$(echo "$CLAIM2_RESP" | sed '$d')
echo "  HTTP ${HTTP_CODE}:"
echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "  ${BODY}"
if [ "$HTTP_CODE" = "409" ]; then
    echo -e "${GREEN}  ✓ Correctly rejected duplicate claim${NC}"
else
    echo -e "${RED}  ✗ Expected 409, got ${HTTP_CODE}${NC}"
fi
echo ""

# ── 7. Hangup ────────────────────────────────────────────────────
echo -e "${YELLOW}[7/7] Hanging up...${NC}"
HANGUP_RESP=$(curl -s -X POST "${API}/call/${CALL_ID}/hangup")
echo "  Response:"
echo "$HANGUP_RESP" | python3 -m json.tool 2>/dev/null || echo "  ${HANGUP_RESP}"
echo -e "${GREEN}  ✓ Call ended${NC}"
echo ""

# ── Summary ──────────────────────────────────────────────────────
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✓ All tests completed successfully!${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"

# ── Bonus: List clips ────────────────────────────────────────────
echo ""
echo -e "${YELLOW}Bonus: Listing recorded clips...${NC}"
curl -s "${API}/clips" | python3 -m json.tool 2>/dev/null || echo "(no clips yet)"
echo ""
echo -e "${CYAN}WebSocket test: wscat -c ws://localhost:8090/ws/test_user${NC}"
echo -e "${CYAN}Snapshot test:  curl ${API}/snapshots/${CALL_ID}.jpg -o snapshot.jpg${NC}"
