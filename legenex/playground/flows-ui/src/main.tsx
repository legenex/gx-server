// Entry point of the Creative Flows island.
//
// Vite builds this into ../web/flows. The vanilla Playground page
// (web/js/pages/flows.js) imports the built module and calls `mount()` with a
// host object: everything that touches the Playground shell (HTTP with the
// session cookie and the CSRF token, toasts, the Library picker, uploads,
// navigation) is handed in, so this bundle never talks to the network itself.
import '@xyflow/react/dist/style.css';
import './styles.css';

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import type { Root } from 'react-dom/client';

import { App } from './App';
import type { Host } from './types';

export type { Host } from './types';

/** Render the editor into `container`. The returned function unmounts it. */
export function mount(container: HTMLElement, host: Host): () => void {
  const root: Root = createRoot(container);
  root.render(
    <StrictMode>
      <App host={host} />
    </StrictMode>,
  );
  return () => {
    // React 19 warns when a root is unmounted during its own render pass.
    queueMicrotask(() => { root.unmount(); });
  };
}

export default { mount };
