"""Conservative sampled zero-speed detector; queue/unknown never accumulate time."""
class LiveTracker:
    def __init__(self, threshold, max_gap):
        self.threshold, self.max_gap = threshold, max_gap
        self.previous = {}

    def clear(self):
        self.previous.clear()

    def forget(self, task_id):
        self.previous.pop(task_id, None)

    def update(self, rows, now):
        previous, current, output = self.previous, {}, []
        for row in rows:
            task_id = row['id']
            downloading = row.get('active') is True and row.get('status') == 5
            zero = downloading and type(row.get('speed')) is int and row['speed'] == 0
            identity = (row.get('name'), row.get('save_path'))
            prev = previous.get(task_id)
            since = now
            if (zero and prev and prev['zero'] and prev['identity'] == identity
                and 0 < now-prev['seen'] <= self.max_gap and prev['received'] == row['received']):
                since = prev['since']
            elapsed = now-since if zero else 0
            state = ('downloading' if downloading else 'queued' if row.get('status') in (1,2,3,4)
                     else 'paused' if row.get('status') == 7 else 'other')
            reason = ('连续零速计时中' if zero else '正在传输' if downloading and row.get('speed') is not None
                      else '速度未知，禁止删除' if downloading else '非正在下载，禁止删除')
            eligible = zero and elapsed >= self.threshold
            current[task_id] = dict(zero=zero, identity=identity, seen=now, since=since, received=row['received'])
            output.append({**row, 'live_state':state, 'estimated_bps':row.get('speed'),
                           'unchanged_seconds':int(elapsed), 'needs_review':eligible,
                           'delete_eligible':eligible, 'protection_reason': '达到零速门槛，等待复查' if eligible else reason})
        self.previous = current
        return output
