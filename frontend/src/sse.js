// Server-sent events from a streamed response body: `data: {json}` lines, events separated
// by a blank line. push() takes decoded text as it arrives and returns the complete events.
export class EventReader {
  constructor() {
    this.buffer = '';
  }

  push(text) {
    this.buffer += text.replace(/\r\n?/g, '\n');
    const events = [];
    let end;
    while ((end = this.buffer.indexOf('\n\n')) !== -1) {
      const block = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      const data = block.split('\n').filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).replace(/^ /, '')).join('\n');
      if (!data) continue;
      try {
        events.push(JSON.parse(data));
      } catch {
        // a malformed event is skipped; the turn's record stays the source of truth
      }
    }
    return events;
  }
}
