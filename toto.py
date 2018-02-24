import json

with open('/tmp/hg.log', 'r') as In:
    data = json.load(In)

new = [{'author': i['author']} for i in data]
with open('/tmp/hg.json', 'w') as Out:
    json.dump(new, Out)
