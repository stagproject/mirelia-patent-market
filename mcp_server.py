import os
import json
import time
import requests
import uvicorn
from typing import Optional
from pydantic import Field
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from supabase import create_client, Client
from dotenv import load_dotenv

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from web3 import Web3
try:
    # web3.py v6+ (Polygon等で必須)
    from web3.middleware import ExtraDataToPOAMiddleware as poa_middleware
except ImportError:
    # 旧バージョン互換用
    from web3.middleware import geth_poa_middleware as poa_middleware


load_dotenv()

security_settings = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp = FastMCP("Mirelia-Patent-Marketplace", transport_security=security_settings)

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
if url and key:
    supabase: Client = create_client(url, key)
else:
    supabase = None

# 各ネットワークのRPC初期化
rpc_base = os.environ.get("BASE_MAINNET")
rpc_polygon = os.environ.get("POLYGON_MAINNET")
rpc_oasis = os.environ.get("OASIS_MAINNET")

usdc_base_raw = os.environ.get("BASE_USDC")
usdc_polygon_raw = os.environ.get("POLYGON_USDC")
abi_string = os.environ.get("ERC20_ABI")
WALLET_ADDRESS_RAW = os.environ.get("SELLER_WALLET_ADDRESS")

chains = {}
if Web3 and abi_string and WALLET_ADDRESS_RAW:
    WALLET_ADDRESS = Web3.to_checksum_address(WALLET_ADDRESS_RAW)
    ERC20_ABI = json.loads(abi_string)
    
    if rpc_base and usdc_base_raw:
        w3_base = Web3(Web3.HTTPProvider(rpc_base))
        chains["base"] = {
            "w3": w3_base,
            "usdc": w3_base.eth.contract(address=Web3.to_checksum_address(usdc_base_raw), abi=ERC20_ABI),
            "type": "erc20"
        }
    
    if rpc_polygon and usdc_polygon_raw:
        w3_polygon = Web3(Web3.HTTPProvider(rpc_polygon))
        w3_polygon.middleware_onion.inject(poa_middleware, layer=0)
        chains["polygon"] = {
            "w3": w3_polygon,
            "usdc": w3_polygon.eth.contract(address=Web3.to_checksum_address(usdc_polygon_raw), abi=ERC20_ABI),
            "type": "erc20"
        }
        
    if rpc_oasis:
        w3_oasis = Web3(Web3.HTTPProvider(rpc_oasis))
        chains["oasis"] = {
            "w3": w3_oasis,
            "usdc": None,
            "type": "native"
        }
else:
    WALLET_ADDRESS = None

# -----------------------------------------------------------------------------
# 1. 探索・詳細確認・重複確認 統合ツール (Hybrid Discovery)
# -----------------------------------------------------------------------------
@mcp.tool()
def search_packages(search_query: str = Field(default="", description="Search query. Leave blank for all packages.")) -> str:
    """
    [COST: FREE]
    The primary marketplace exploration tool.
    - If search_query is empty: Returns the full lightweight inventory (tags, titles, tech_stacks, prices) sorted by tag.
    - If search_query is provided: Returns detailed info including 'description' and 'patent_ids' for deduplication.
    """
    if not supabase:
        return json.dumps({"error": "Database connection failed"})

    try:
        if search_query in ["null", "None", ""]:
            search_query = ""
            
        search_query = search_query.strip()

        if not search_query:
            res = supabase.table("v_package_marketplace") \
                .select("package_tag, category, title, record_count, price_usd, avg_importance_p, tech_stacks") \
                .order("package_tag") \
                .execute()
            return json.dumps(res.data, ensure_ascii=False)
        else:
            safe_query = search_query.replace(",", " ")
            res = supabase.table("v_catalogs") \
                .select("*") \
                .or_(f"category.ilike.%{safe_query}%,title.ilike.%{safe_query}%,description.ilike.%{safe_query}%,package_tag.ilike.%{safe_query}%") \
                .order("package_tag") \
                .execute()
            return json.dumps(res.data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})

# -----------------------------------------------------------------------------
# 2. 決済・配信: 最終実行 (堅牢版ロジックを完全維持)
# -----------------------------------------------------------------------------
@mcp.tool()
def verify_crypto_payment_and_deliver(tx_hash: str, package_tag: str, network: str) -> str:
    """
    [COST: PAID / FINAL EXECUTION]
    Verifies an on-chain cryptocurrency transaction and delivers the full JSON dataset CDN URL. 
    WARNING: Do NOT call this tool until you have successfully executed the blockchain transaction and obtained a valid 'tx_hash'. 
    """
    if not supabase:
        return json.dumps({"error": "Supabase connection failed"}, ensure_ascii=False)
        
    network = network.lower()
    if network not in chains:
        return json.dumps({"error": f"Unsupported or unconfigured network: {network}"}, ensure_ascii=False)
        
    tx_check = supabase.table("processed_transactions").select("tx_hash").eq("tx_hash", tx_hash).execute()
    if tx_check.data:
        return json.dumps({"error": "Transaction has already been processed."}, ensure_ascii=False)

    chain_info = chains[network]
    w3 = chain_info["w3"]

    try:
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            return json.dumps({"error": "Invalid transaction hash or receipt not found"}, ensure_ascii=False)

        if receipt['status'] != 1:
            return json.dumps({"error": "Transaction failed on-chain"}, ensure_ascii=False)

        block = None
        for _ in range(5):
            try:
                block = w3.eth.get_block(receipt['blockNumber'])
                break
            except Exception:
                time.sleep(2)
                
        if not block:
            return json.dumps({"error": "Block not found due to RPC sync delay. Please retry verification."}, ensure_ascii=False)

        current_time = int(time.time())
        if current_time - block['timestamp'] > 3600:
            return json.dumps({"error": "Transaction is expired. Must be executed within the last 1 hour."}, ensure_ascii=False)

        catalog_res = supabase.table("patent_packages").select("price_usd, sales_count").eq("package_tag", package_tag).execute()
        if not catalog_res.data:
            return json.dumps({"error": "Package not found"}, ensure_ascii=False)
            
        catalog_data = catalog_res.data[0]
        real_price_usd = float(catalog_data['price_usd'])
        
        payment_found = False
        
        if chain_info["type"] == "erc20":
            required_usdc = int(real_price_usd * (10**6))
            events = chain_info["usdc"].events.Transfer().process_receipt(receipt)
            for event in events:
                if event['args']['to'].lower() == WALLET_ADDRESS.lower() and event['args']['value'] >= required_usdc:
                    payment_found = True
                    break
        elif chain_info["type"] == "native":
            tx = w3.eth.get_transaction(tx_hash)
            if tx['to'] and tx['to'].lower() == WALLET_ADDRESS.lower():
                try:
                    res_cg = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=oasis-network&vs_currencies=usd", timeout=5)
                    rose_price = float(res_cg.json()['oasis-network']['usd'])
                    if rose_price > 0:
                        required_wei = int((real_price_usd / rose_price) * 0.95 * (10**18))
                        if tx['value'] >= required_wei:
                            payment_found = True
                except Exception as e:
                    return json.dumps({"error": str(e)}, ensure_ascii=False)

        if not payment_found:
             return json.dumps({"error": "Valid payment not found or insufficient amount."}, ensure_ascii=False)
             
        try:
            supabase.table("processed_transactions").insert({
                "tx_hash": tx_hash,
                "network": network,
                "package_tag": package_tag,
                "verified_at": current_time
            }).execute()
        except Exception:
            return json.dumps({"error": "Race condition: Transaction already processed."}, ensure_ascii=False)
             
        supabase.table("patent_packages").update({"sales_count": (catalog_data['sales_count'] or 0) + 1}).eq("package_tag", package_tag).execute()

        res_data = supabase.table("v_patent_marketplace_lite").select("*").contains("package_tags", [package_tag]).execute()
        
        return json.dumps({
            "system_log": f"Payment verified on {network.upper()}.",
            "package_data": res_data.data
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# -----------------------------------------------------------------------------
# Starlette / FastMCP SSE Endpoint Setup
# -----------------------------------------------------------------------------
sse_app = None
if hasattr(mcp, "get_starlette_app"):
    sse_app = mcp.get_starlette_app()
elif hasattr(mcp, "sse_app"):
    sse_app = mcp.sse_app()
elif hasattr(mcp, "_create_sse_app"):
    sse_app = mcp._create_sse_app()

routes = []
if sse_app:
    routes.append(Mount("/", app=sse_app))

app = Starlette(routes=routes)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_server_card(request):
    return JSONResponse({
        "serverInfo": {
            "name": "Mirelia-Patent-Marketplace",
            "version": "1.2.1-A2A-Optimized"
        },
        "tools": [
            {
                "name": "search_packages",
                "description": "[COST: FREE] Discovery tool. No args = Lite Inventory sorted by tag. Query = Detailed info + Patent IDs.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "search_query": {"type": "string", "description": "Optional: Keyword or category code."}
                    }
                }
            },
            {
                "name": "verify_crypto_payment_and_deliver",
                "description": "[COST: PAID] Verifies on-chain payment and delivers full dataset.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tx_hash": {"type": "string", "description": "Transaction hash"},
                        "package_tag": {"type": "string", "description": "Purchased package tag"},
                        "network": {"type": "string", "description": "Network: 'base', 'polygon', or 'oasis'"}
                    },
                    "required": ["tx_hash", "package_tag", "network"]
                }
            }
        ]
    })

app.add_route("/.well-known/mcp/server-card.json", get_server_card, methods=["GET", "OPTIONS"])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")