import json
from collections import Counter

with open('django_graph_v4.json') as f:
    g4 = json.load(f)
with open('django_wagtail_graph_v1.json') as f:
    g1 = json.load(f)

tc4 = Counter(n['type'] for n in g4['nodes'])
ec4 = Counter(e['type'] for e in g4['edges'])
print('=== v4 (django only, no graph metrics) ===')
for t, c in sorted(tc4.items()):
    print(f'  {t:<12} {c:,}')
print(f'  Total edges: {sum(ec4.values()):,}')

dj1_nodes = [n for n in g1['nodes'] if '::django::' in n['id']]
wag1_nodes = [n for n in g1['nodes'] if '::wagtail::' in n['id']]
tc1 = Counter(n['type'] for n in dj1_nodes)
print()
print('=== v1 django partition ===')
for t, c in sorted(tc1.items()):
    print(f'  {t:<12} {c:,}')

def indegree(edges, node_ids):
    d = Counter()
    for e in edges:
        if e['target'] in node_ids:
            d[e['target']] += 1
    return d

dj4_ids = {n['id'] for n in g4['nodes']}
dj1_ids = {n['id'] for n in dj1_nodes}
ind4 = indegree(g4['edges'], dj4_ids)
ind1 = indegree(g1['edges'], dj1_ids)

gains = []
for nid in dj1_ids:
    old = ind4.get(nid, 0)
    new = ind1.get(nid, 0)
    if old > 0 and new > old:
        gains.append((new - old, new / old, nid, old, new))
gains.sort(reverse=True)

print()
print('Top 20 Django nodes by in-degree gain from adding Wagtail:')
print(f'  {"gain":>5}  {"old":>5} -> {"new":>5}  node')
for delta, ratio, nid, old, new in gains[:20]:
    label = '::'.join(nid.split('::')[2:])
    print(f'  {delta:>5}  {old:>5} -> {new:>5}  {label}')

print()
print('Top 15 nodes by PageRank in v1 (functions only):')
fn_pr = [(n['features']['pagerank'], n['id'])
         for n in g1['nodes']
         if n['type'] == 'function' and n.get('features', {}).get('pagerank') is not None]
fn_pr.sort(reverse=True)
for pr, nid in fn_pr[:15]:
    label = '::'.join(nid.split('::')[1:])
    print(f'  {pr:.6f}  {label}')

print()
print('Top 10 nodes by PageRank in v1 (modules only):')
mod_pr = [(n['features']['pagerank'], n['id'])
          for n in g1['nodes']
          if n['type'] == 'module' and n.get('features', {}).get('pagerank') is not None]
mod_pr.sort(reverse=True)
for pr, nid in mod_pr[:10]:
    label = '::'.join(nid.split('::')[1:])
    print(f'  {pr:.6f}  {label}')

print()
leiden_vals = [n['features']['leiden_community'] for n in g1['nodes']
               if n.get('features', {}).get('leiden_community') is not None]
lc = Counter(leiden_vals)
isolates = lc.get(-1, 0)
real_communities = len([k for k in lc if k >= 0])
top5 = sorted([(k, v) for k, v in lc.items() if k >= 0], key=lambda x: -x[1])[:5]
print(f'Leiden: {real_communities} communities, {isolates:,} isolates')
print(f'Top 5 community sizes: {[(c, s) for c, s in top5]}')

print()
wag_pr = [(n['features']['pagerank'], n['id'])
          for n in wag1_nodes
          if n.get('features', {}).get('pagerank') is not None]
wag_pr.sort(reverse=True)
print('Top 10 Wagtail nodes by PageRank:')
for pr, nid in wag_pr[:10]:
    label = '::'.join(nid.split('::')[1:])
    print(f'  {pr:.6f}  {label}')

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
