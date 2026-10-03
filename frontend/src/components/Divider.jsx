// i18n: migrated
// A column divider: drag it, double-click it to reset, or focus it and use the arrow keys.
import { STEP } from '../layout.js';

export function Divider({ label, value, min, max, onMove, onDone, onReset }) {
  function down(event) {
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const move = (moved) => onMove({ x: moved.clientX });
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      document.body.classList.remove('dragging');
      onDone();
    };
    document.body.classList.add('dragging');
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  function key(event) {
    const steps = { ArrowLeft: -STEP, ArrowRight: STEP, Home: -Infinity, End: Infinity };
    if (!(event.key in steps)) return;
    event.preventDefault();
    onMove({ step: steps[event.key] });
    onDone();
  }

  return (
    <div role="separator" aria-orientation="vertical" aria-label={label} tabIndex={0}
      aria-valuenow={Math.round(value)} aria-valuemin={Math.round(min)} aria-valuemax={Math.round(max)}
      className="divider-handle" onPointerDown={down} onDoubleClick={onReset} onKeyDown={key} />
  );
}
