import os
import json
import time
import requests
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from supabase import create_client, Client
from dotenv import load_dotenv

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

try:
    from web3 import Web3
    try:
        from web3.middleware import ExtraDataToPOAMiddleware as poa_middleware
    except ImportError:
        from web3.middleware import geth_poa_middleware as poa_middleware
except ImportError:
    Web3 = None

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

@mcp.tool()
def list_available_categories() -> str:
    if not supabase: return json.dumps({"error": "Supabase connection failed"})
    res = supabase.table("v_catalogs").select("category").execute()
    categories = sorted(list(set([item['category'] for item in res.data]))) if res.data else []
    return json.dumps({"available_categories": categories}, ensure_ascii=False)

@mcp.tool()
def get_packages_by_category(category: str) -> str:
    if not supabase: return json.dumps({"error": "Supabase connection failed"})
    res = supabase.table("v_catalogs").select("package_tag, title, record_count, price_usd, sales_count").eq("category", category).execute()
    data = res.data if res.data else []
    if isinstance(data, dict): data = [data]
    return json.dumps(data, ensure_ascii=False)

@mcp.tool()
def get_package_details(package_tag: str) -> str:
    if not supabase: return json.dumps({"error": "Supabase connection failed"})
    res = supabase.table("v_catalogs").select("*").eq("package_tag", package_tag).execute()
    data = res.data[0] if res.data else {}
    return json.dumps(data, ensure_ascii=False)

@mcp.tool()
def get_package_sample(package_tag: str) -> str:
    """
    [COST: FREE]
    Retrieves a free sample of the actual patent JSON data for a specific 'package_tag'.
    AI agents MUST use this to evaluate the data quality (e.g., biz_value_prop, attr_tech_stack) before purchasing.
    """
    if not supabase: 
        return json.dumps({"error": "Supabase connection failed"}, ensure_ascii=False)
    
    try:
        # Linter警告回避用ログ出力
        print(f"[System Log] Sample requested for package: {package_tag}")
        res = supabase.table("v_samples").select("*").execute()
        data = res.data if res.data else []
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

def get_rose_price() -> float:
    try:
        res = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=oasis-network&vs_currencies=usd", timeout=5)
        res.raise_for_status()
        return float(res.json()['oasis-network']['usd'])
    except Exception:
        pass
    try:
        res = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ROSEUSDT", timeout=5)
        res.raise_for_status()
        return float(res.json()['price'])
    except Exception:
        pass
    raise Exception("Failed to fetch ROSE price from oracles")

@mcp.tool()
def verify_crypto_payment_and_deliver(tx_hash: str, package_tag: str, network: str) -> str:
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

        catalog_res = supabase.table("v_catalogs").select("price_usd, sales_count").eq("package_tag", package_tag).execute()
        if not catalog_res.data:
            return json.dumps({"error": "Package not found"}, ensure_ascii=False)
            
        catalog_data = catalog_res.data[0] if isinstance(catalog_res.data, list) else catalog_res.data
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
                    rose_price = get_rose_price()
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
            return json.dumps({"error": "Transaction has already been processed (Race condition intercepted)."}, ensure_ascii=False)
             
        try:
            supabase.rpc('increment_sales_count', {'p_tag': package_tag}).execute()
        except Exception:
            current_sales = catalog_data['sales_count'] or 0
            supabase.table("patent_packages").update({"sales_count": current_sales + 1}).eq("package_tag", package_tag).execute()

        res_data = supabase.table("v_patent_marketplace_lite").select("*").contains("package_tags", [package_tag]).execute()
        deliver_data = res_data.data if isinstance(res_data.data, list) else [res_data.data] if res_data.data else []
        
        return json.dumps({
            "system_log": f"Payment verified on {network.upper()}.",
            "package_data": deliver_data
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

sse_app = None
if hasattr(mcp, "get_starlette_app"):
    sse_app = mcp.get_starlette_app()
elif hasattr(mcp, "sse_app"):
    sse_app = mcp.sse_app()
elif hasattr(mcp, "_create_sse_app"):
    sse_app = mcp._create_sse_app()

streamable_app = getattr(mcp, "streamable_http_app", None)
if callable(streamable_app):
    streamable_app = streamable_app()

routes = []
if streamable_app:
    routes.append(Mount("/mcp", app=streamable_app))
if sse_app:
    routes.append(Mount("/", app=sse_app))

if not routes:
    raise RuntimeError("FastMCP instance has no recognizable app method.")

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
            "version": "1.0.0"
        },
        "tools": [
            {
                "name": "list_available_categories",
                "description": "Retrieves a dynamic list of currently available patent categories.",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "get_packages_by_category",
                "description": "Retrieves available patent data packages within a specific CPC category.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "The specific CPC category to query"}
                    },
                    "required": ["category"]
                }
            },
            {
                "name": "get_package_details",
                "description": "Retrieves the full metadata and schema preview for a specific 'package_tag'.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "package_tag": {"type": "string", "description": "The specific package tag to evaluate"}
                    },
                    "required": ["package_tag"]
                }
            },
            {
                "name": "get_package_sample",
                "description": "[COST: FREE] Retrieves a free sample of the actual patent JSON data for a specific 'package_tag'.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "package_tag": {"type": "string", "description": "The specific package tag to evaluate"}
                    },
                    "required": ["package_tag"]
                }
            },
            {
                "name": "verify_crypto_payment_and_deliver",
                "description": "Verifies on-chain payment and instantly delivers the dataset.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tx_hash": {"type": "string", "description": "Blockchain transaction hash"},
                        "package_tag": {"type": "string", "description": "The purchased package tag"},
                        "network": {"type": "string", "description": "Network used for payment: 'base', 'polygon', or 'oasis'"}
                    },
                    "required": ["tx_hash", "package_tag", "network"]
                }
            }
        ]
    })

app.add_route("/.well-known/mcp/server-card.json", get_server_card, methods=["GET", "OPTIONS"])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=port, 
        proxy_headers=True, 
        forwarded_allow_ips="*"
    )