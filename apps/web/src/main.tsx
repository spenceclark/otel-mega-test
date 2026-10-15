import './telemetry'; // first import: providers and the fetch patch exist before anything else runs
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { AskPage } from './AskPage';

createRoot(document.getElementById('root')!).render(
    <StrictMode>
        <AskPage />
    </StrictMode>,
);
