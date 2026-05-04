.PHONY: install build test clean fmt deploy-base-sepolia deploy-op-sepolia deploy-lux-testnet verify-base-sepolia

# ── Setup ──────────────────────────────────────────────────────────────────

install:
	forge install

build:
	forge build

clean:
	forge clean

fmt:
	forge fmt

test:
	forge test -vv

# ── Deploy ─────────────────────────────────────────────────────────────────
# Each target loads .env, then runs the Deploy script with --broadcast --verify.
# Override RPC with RPC_<CHAIN>_OVERRIDE=... if you need a private endpoint.

deploy-base-sepolia:
	@test -f .env || (echo ".env not found — copy .env.example" && exit 1)
	. ./.env && forge script script/Deploy.s.sol:Deploy \
	  --rpc-url $$RPC_BASE_SEPOLIA \
	  --broadcast \
	  --verify \
	  --etherscan-api-key $$ETHERSCAN_API_KEY \
	  -vvv

deploy-op-sepolia:
	@test -f .env || (echo ".env not found — copy .env.example" && exit 1)
	. ./.env && forge script script/Deploy.s.sol:Deploy \
	  --rpc-url $$RPC_OP_SEPOLIA \
	  --broadcast \
	  --verify \
	  --etherscan-api-key $$ETHERSCAN_API_KEY \
	  -vvv

deploy-lux-testnet:
	@test -f .env || (echo ".env not found — copy .env.example" && exit 1)
	. ./.env && forge script script/Deploy.s.sol:Deploy \
	  --rpc-url $$RPC_LUX_TESTNET \
	  --broadcast \
	  --legacy \
	  -vvv

# ── Verify (re-run if --verify failed during deploy) ───────────────────────

verify-base-sepolia:
	@test -n "$(ADDRESS)" || (echo "usage: make verify-base-sepolia ADDRESS=0x..." && exit 1)
	. ./.env && forge verify-contract \
	  --chain-id 84532 \
	  --etherscan-api-key $$ETHERSCAN_API_KEY \
	  --constructor-args $$(cast abi-encode "constructor(uint256)" 84532) \
	  $(ADDRESS) contracts/AgentEscrow.sol:AgentEscrow
