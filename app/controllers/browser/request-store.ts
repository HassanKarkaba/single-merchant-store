import { Dict } from './contracts';
export class BrowserRequestStore {
    epoch = 'anonymous';
    identityRevision = 0;
    generation = 0;
    scope = 'preauth';
    pending = new Map<string, Dict>();
    private requests = new Map<string, number>();
    begin_request(resource: string): {
        epoch: string;
        identityRevision: number;
        generation: number;
        resource: string;
        sequence: number;
    } { const sequence = (this.requests.get(resource) || 0) + 1; this.requests.set(resource, sequence); return { epoch: this.epoch, identityRevision: this.identityRevision, generation: this.generation, resource, sequence }; }
    bind_scope(scope: string): void {
        this.scope = scope;
        this.pending.clear();
        try {
            for (let index = 0; index < sessionStorage.length; index += 1) {
                const key = sessionStorage.key(index);
                if (!key?.startsWith(`sme-operation:${scope}:`))
                    continue;
                const value = JSON.parse(sessionStorage.getItem(key) || '{}');
                if (value.principal_scope === scope && typeof value.id === 'string' && typeof value.resource === 'string' && typeof value.operation === 'string')
                    this.pending.set(value.id, value);
            }
        }
        catch { /* Safe references remain optional; no credential enters storage. */ }
    }
    can_publish(ticket: {
        epoch: string;
        identityRevision: number;
        generation: number;
        resource: string;
        sequence: number;
    }): boolean { return ticket.identityRevision === this.identityRevision && ticket.epoch === this.epoch && ticket.generation === this.generation && this.requests.get(ticket.resource) === ticket.sequence; }
    invalidate(_resource: string): void { this.generation += 1; }
    on_session_changed(epoch: string): void { this.identityRevision += 1; this.epoch = epoch; this.scope = 'preauth'; this.pending.clear(); this.requests.clear(); this.generation += 1; }
    remember(id: string, value: Dict): void {
        this.pending.set(id, value);
        try {
            sessionStorage.setItem(`sme-operation:${this.scope}:${id}`, JSON.stringify({ id, principal_scope: this.scope, resource: value.resource, operation: value.operation, operation_id: value.operation_id || null, started_at: value.started_at }));
        }
        catch { /* Optional storage failure never loses server ownership. */ }
    }
    settle(id: string, operationId: string | null, uncertain: boolean): void {
        const value = this.pending.get(id);
        if (uncertain) {
            if (value)
                this.remember(id, { ...value, operation_id: operationId });
        }
        else {
            this.pending.delete(id);
            try {
                sessionStorage.removeItem(`sme-operation:${this.scope}:${id}`);
            }
            catch { /* Browser storage may be disabled. */ }
        }
    }
}
