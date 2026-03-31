import os
import requests
import json
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from supabase import create_client, Client
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

security_settings = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp = FastMCP("Mirelia-Patent-Marketplace", transport_security=security_settings)

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(url, key)

rpc_url = os.environ.get("META_RPC_URL")
private_key = os.environ.get("META_PRIVATE_KEY")
if rpc_url and private_key:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    account = w3.eth.account.from_key(private_key)
    WALLET_ADDRESS = account.address
else:
    w3 = None
    WALLET_ADDRESS = None

def get_rose_usd_price():
    res = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=oasis-network&vs_currencies=usd")
    res.raise_for_status()
    return float(res.json()["oasis-network"]["usd"])

@mcp.tool()
def list_available_categories() -> str:
    """Returns a JSON string containing an array of all available patent categories."""
    res = supabase.table("v_catalogs").select("category").execute()
    categories = sorted(list(set([item['category'] for item in res.data]))) if res.data else []
    return json.dumps({"available_categories": categories}, ensure_ascii=False)

@mcp.tool()
def get_packages_by_category(category: str) -> str:
    """
    Returns a JSON string containing an array of patent packages in the specified category.
    """
    res = supabase.table("v_catalogs").select("package_tag, title, record_count, price_usd, sales_count").eq("category", category).execute()
    
    if not res.data:
        data = []
    elif isinstance(res.data, dict):
        data = [res.data]
    else:
        data = res.data
        
    return json.dumps(data, ensure_ascii=False)

@mcp.tool()
def get_package_details(package_tag: str) -> str:
    """Returns a JSON string with the full details of a specific patent package."""
    res = supabase.table("v_catalogs").select("*").eq("package_tag", package_tag).execute()
    data = res.data[0] if res.data else {}
    return json.dumps(data, ensure_ascii=False)

@mcp.tool()
def verify_crypto_payment_and_deliver(tx_hash: str, package_tag: str) -> str:
    """
    Verifies payment and delivers the data. 
    Returns a JSON object with 'system_log' and 'package_data' array.
    """
    if not w3 or not w3.is_connected(): return json.dumps({"error": "Web3 connection failed"})

    try:
        try:
            tx = w3.eth.get_transaction(tx_hash)
        except Exception:
            return json.dumps({"error": "Invalid transaction hash"})

        if tx['to'].lower() != WALLET_ADDRESS.lower():
            return json.dumps({"error": "Invalid destination wallet address"})

        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if receipt['status'] != 1:
            return json.dumps({"error": "Transaction failed"})

        catalog_res = supabase.table("v_catalogs").select("price_usd, sales_count").eq("package_tag", package_tag).execute()
        if not catalog_res.data:
            return json.dumps({"error": "Package not found"})
            
        catalog_data = catalog_res.data[0] if isinstance(catalog_res.data, list) else catalog_res.data
        real_price_usd = float(catalog_data['price_usd'])
        current_sales = catalog_data['sales_count'] or 0
        rose_usd_rate = get_rose_usd_price()
        
        # ========== 削除部分（テスト稼働用）：ここから ==========
        # test_price_usd = 0.001
        # required_wei = w3.to_wei(test_price_usd / rose_usd_rate, 'ether')
        # if tx['value'] < required_wei * 0.90:
        #     return json.dumps({"error": "Insufficient funds (Test price $0.001)"})
        # log_msg = f"Approved at test price ${test_price_usd}. Original price was ${real_price_usd}. Sales count updated to {current_sales + 1}."
        # ========== 削除部分（テスト稼働用）：ここまで ==========

        # ========== コメントアウト解除部分（本番稼働用）：ここから ==========
        required_wei = w3.to_wei(real_price_usd / rose_usd_rate, 'ether')
        if tx['value'] < required_wei * 0.90:
             return json.dumps({"error": f"Insufficient funds. Price is ${real_price_usd}"})
        log_msg = f"Payment of ${real_price_usd} verified. Sales count updated to {current_sales + 1}."
        # ========== コメントアウト解除部分（本番稼働用）：ここまで ==========

        supabase.table("patent_packages").update({"sales_count": current_sales + 1}).eq("package_tag", package_tag).execute()
        res_data = supabase.table("v_patent_marketplace_lite").select("*").contains("package_tags", [package_tag]).execute()
        deliver_data = res_data.data if isinstance(res_data.data, list) else [res_data.data] if res_data.data else []
        
        return json.dumps({
            "system_log": log_msg,
            "package_data": deliver_data
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    if hasattr(mcp, "get_starlette_app"):
        app = mcp.get_starlette_app()
    elif hasattr(mcp, "sse_app"):
        app = mcp.sse_app()
    else:
        raise RuntimeError("FastMCP instance has no recognizable app method.")
    uvicorn.run(app, host="0.0.0.0", port=port)