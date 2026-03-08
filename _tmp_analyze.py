import json, collections

d = json.load(open("/Users/alejandromarchini/Documents/MSAAI/capstone/source-code-RGAT/django_graph.json"))
total_calls = sum(1 for e in d["edges"] if e["type"] in ("CALLS","CALLS_SYMBOL"))
sym = [e for e in d["edges"] if e["type"] == "CALLS_SYMBOL"]

cats = collections.Counter()
for e in sym:
    text = e["target"].split("::",3)[-1]
    scope = e["target"].split("::")[2]
    parts = text.split(".")
    if text == "<dynamic>":
        cats["dynamic"] += 1
    elif len(parts) == 1:
        cats["single_name"] += 1
    elif parts[0] == "self" and len(parts) == 2:
        cats["self.method"] += 1
    elif parts[0] == "self" and len(parts) >= 3:
        cats["self.attr.method"] += 1
    elif parts[0] == "cls":
        cats["cls.method"] += 1
    elif scope == "global" and len(parts) >= 2:
        cats["var.method"] += 1
    else:
        cats["other"] += 1

print(f"Total calls (resolved+unresolved): {total_calls}")
print(f"Resolved CALLS: {total_calls - len(sym)} ({(total_calls-len(sym))*100//total_calls}%)")
print(f"CALLS_SYMBOL:   {len(sym)} ({len(sym)*100//total_calls}%)")
print()
print("CALLS_SYMBOL breakdown:")
for k,v in cats.most_common():
    print(f"  {k:<20} {v:>6}  ({v*100//len(sym)}%)")
