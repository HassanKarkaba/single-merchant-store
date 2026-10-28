import {createCommandId} from './ids';
import {ApiError} from './errors';

/** The recovery identity is transient. It retains no password or bearer token. */
export class ResetIntentIdentity {
    private current: {digest:string; id:string} | null = null;
    private uncertain = false;
    private revision = 0;
    constructor(private readonly createId:()=>string = createCommandId) {}
    async identityFor(token:string, password:string): Promise<{id:string; reused:boolean}> {
        if (!globalThis.crypto?.subtle) throw new ApiError('This browser cannot protect the account request identity.',503,'unsupported_environment');
        const revision=this.revision;
        const bytes = new TextEncoder().encode(JSON.stringify(['password_reset',token,password]));
        let result:ArrayBuffer;
        try {result = await crypto.subtle.digest('SHA-256',bytes);} finally {bytes.fill(0);}
        if (revision!==this.revision) throw new ApiError('A newer account link replaced this request. Review the current form.',409,'stale_account_intent');
        const digest = Array.from(new Uint8Array(result), value=>value.toString(16).padStart(2,'0')).join('');
        if (this.uncertain && this.current?.digest===digest) return {id:this.current.id,reused:true};
        const id = this.createId();
        this.current = {digest,id}; this.uncertain=false;
        return {id,reused:false};
    }
    markUncertain(): void {if (this.current) this.uncertain=true;}
    clear(): void {this.revision+=1; this.current=null; this.uncertain=false;}
}
