import json
import os

new_card = {
    'id': 'sensenova-vision',
    'title': 'SenseNova-Vision',
    'subtitle': 'Vision as Unified Multimodal Generation',
    'image': 'cards/sensenova-vision-cover.svg',
    'page': 'pages/sensenova.html',
    'tags': ['商汤', '视觉生成', 'MoT', '统一多模态', '7B'],
    'date': '2026-07-22',
    'type': 'recommendation'
}

path = '/var/www/youngsinsight/cards.json'
data = json.load(open(path))
idx = next((i for i, c in enumerate(data) if c.get('id') == 'sensenova-vision'), None)
if idx is not None:
    data[idx] = new_card
else:
    data.append(new_card)
json.dump(data, open(path, 'w'), ensure_ascii=False, indent=2)
print(f'Done: {len(data)} cards')
