import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App.jsx';
import { startSession } from './api.js';
import './index.css';

startSession();
createRoot(document.getElementById('root')).render(<StrictMode><App /></StrictMode>);
