# Security and private data

Please report security concerns privately to the repository owner through their GitHub profile. Do not post credentials, raw webhook bodies, owner messages, financial records or calendar details in a public issue or pull request. Private vulnerability reporting may be used if it is available on this repository.

The application authenticates webhook signatures and a single configured owner before provider work. Delivered review versions/nonces authorize writes; model tool proposals and conversation history do not. Transport allowlists bind resources and methods. Durable action IDs and positive reconciliation protect against duplicate and uncertain writes.

Raw conversation records contain private information and are retained independently of the recent model-context window. Decide access and retention policy before a live deployment; context expiry is not automatic data deletion. Do not enable destructive TTL or snapshot restoration as a rollback shortcut.

Never commit `.env`, credential/key files, local databases, private fixtures, screenshots, transcripts, runtime logs, build archives, release evidence or approval records. Ignore rules are a guard, not a complete publication audit: inspect tracked files, reachable Git history/refs, repository metadata and uploaded artifacts before changing visibility.

If a real secret enters Git, stop publication and arrange rotation/removal privately. Removing a file in a later commit does not erase its history.
