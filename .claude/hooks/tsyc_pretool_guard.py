#!/usr/bin/env python3
from __future__ import annotations
import json,re,sys
PROD={"collect_one_facebook_post.py","clean_facebook_raw_pages.py","create_candidates_from_cleaned_posts.py","upload_facebook_images_to_supabase.py","register_reference_source.py","collect_reference_metadata.py","manual_create_product_reference.py","match_candidate_identity.py","create_internal_product.py","review_product_images.py","prepare_product_content.py","create_woocommerce_draft.py","sync_woocommerce_product_status.py"}
def emit(d,r):
 print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":d,"permissionDecisionReason":r}}))
def main():
 try:e=json.loads(sys.stdin.read())
 except Exception: emit("deny","TSYC guard could not parse request."); return
 if e.get("tool_name") not in {"Bash","PowerShell"}: return
 c=str((e.get("tool_input") or {}).get("command","")); n=c.replace("\\","/").lower()
 scripts=set(re.findall(r"(?:^|[\s\"'])scripts/([a-z0-9_.-]+\.py)(?:$|[\s\"'])",n))
 if not scripts:return
 if ".venv/scripts/python.exe" not in n: emit("deny","Use repository .venv/Scripts/python.exe only."); return
 p=sorted(scripts & PROD)
 if p: emit("ask","TSYC production-write gate requires approval: "+", ".join(p))
if __name__=="__main__": main()
