import { Dict } from './contracts';
export class ApiError extends Error {
    code: string;
    details: Dict;
    operationId: string | null;
    status: number;
    constructor(message: string, status = 0, code = 'request_unknown', details: Dict = {}, operationId: string | null = null) { super(message); this.code = code; this.details = details; this.operationId = operationId; this.status = status; }
}
export function errorMessage(error: unknown): string { return error instanceof Error ? error.message : 'The request could not be completed. Please check its status before trying again.'; }
