// What a view shows of the project's papers (the Library's listing, its search, the message box's @-mentions): the answer
// to its newest read asked in the papers' current generation (library.js libraryGeneration), never one asked before a
// change and never one shown after a change before a read asked after it answers (Harold, 2026-10-21: no client-held
// derived state; M2 audit, area 1, design review F option 2.2, pending Harold). key names what is read (null: nothing);
// a change of key or of generation reads again at once, and nothing read for another key or generation is shown.
// reload() reads again within a generation (a poll, a search asked again): what is shown stays until its answer, and a
// failed one keeps it, with the error.
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { libraryGeneration, watchLibrary } from './library.js';

export function useLibraryRead(key, read) {
  const generation = useSyncExternalStore(watchLibrary, libraryGeneration);
  const [held, setHeld] = useState(null); // { key, generation, value, error }: the newest read's answer
  const [asking, setAsking] = useState(null); // { key, generation }: the newest read, while it is unanswered
  const newest = useRef(0);
  const reader = useRef(read);
  reader.current = read;
  const reload = useCallback(() => {
    if (key === null || key === undefined) return;
    const mine = ++newest.current;
    const asked = { key, generation: libraryGeneration() };
    setAsking(asked);
    const answered = (value, error) => {
      if (mine !== newest.current) return;
      setAsking(null);
      setHeld((now) => ({ ...asked, error,
        value: error === null ? value : now?.key === asked.key && now.generation === asked.generation ? now.value : null }));
    };
    let pending;
    try {
      pending = Promise.resolve(reader.current());
    } catch (error) {
      pending = Promise.reject(error);
    }
    pending.then((value) => answered(value, null), (error) => answered(null, error));
  }, [key]);
  useEffect(() => { reload(); }, [reload, generation]);
  const current = (state) => state !== null && state.key === key && state.generation === generation;
  return { value: current(held) ? held.value : null, error: current(held) ? held.error : null, busy: current(asking), reload };
}
