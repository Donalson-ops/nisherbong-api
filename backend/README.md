# Nisherbong hosted backend

The backend is a separate FastAPI service. Supabase provides hosted PostgreSQL,
email/password authentication, email confirmation, and private image storage.
The Flet desktop/mobile client is not connected to this service yet.

The API is assembled in `backend/application.py`. Authentication, administrative
routes, Supabase access, and runtime settings are kept in separate modules;
`backend/main.py` remains the stable Uvicorn entrypoint.

## Supabase project setup

1. Create a Supabase project in the account that should own the hosted database.
2. In **Authentication > Providers > Email**, enable email/password signup and
   email confirmation. Set the allowed site and redirect URLs to the real
   application callback before production.
3. Configure the Supabase Auth SMTP settings with the verified Resend domain
   if confirmation and password-recovery email should use Resend. The backend's
   `RESEND_API_KEY` is for admin-triggered notifications and does not replace
   Supabase Auth SMTP.
4. Apply
   `supabase/migrations/202610260001_initial_schema.sql` using the Supabase SQL
   editor, or link the Supabase CLI to the project and run `supabase db push`.
   The migration creates user-owned tables, Row Level Security policies, and a
   private `user-images` bucket.
5. Register the first admin account through the signup endpoint, confirm its
   email, then promote that exact account from the Supabase SQL editor:

   ```sql
   update public.profiles
   set role = 'admin'
   where id = (
       select id
       from auth.users
       where lower(email) = lower('REPLACE_WITH_ADMIN_EMAIL')
         and email_confirmed_at is not null
   );
   ```

   Verify that exactly one row was updated. Do not expose the service-role key
   to the client or use user-editable signup metadata to assign admin roles.

## Login com Google

O app abre o navegador e recebe o retorno em `http://127.0.0.1:54321/callback`
(OAuth com PKCE). Para funcionar, no painel do Supabase:

1. Crie um *OAuth Client ID* (tipo "Web application") no Google Cloud Console.
   Em "Authorized redirect URIs" informe
   `https://<seu-projeto>.supabase.co/auth/v1/callback`.
2. Em **Authentication > Providers > Google**, ative o provedor e cole o
   Client ID e o Client Secret.
3. Em **Authentication > URL Configuration > Redirect URLs**, adicione
   `http://127.0.0.1:54321/callback`.

Contas Google e contas de email com o mesmo endereço são vinculadas pelo
Supabase. Um primeiro acesso com Google já cria a conta (perfil `user`).

## Run the API

Copy `.env.example` to `.env` for local development and fill it from the
Supabase project's API settings. Keep `.env` out of source control. The
service-role key belongs only in the backend hosting provider's encrypted
environment-variable store.

```powershell
python -m pip install -r backend\requirements.txt
python -m uvicorn backend.main:app --reload --env-file .env
```

The backend container can be built from the project root with
`docker build -f backend\Dockerfile -t nisherbong-backend .`. Set the same
environment variables in the chosen container host. `/health` is public;
authenticated routes validate Supabase access tokens, and admin routes check
the server-managed `profiles.role`.

Available endpoints are documented at `/docs` while the API is running:

- `POST /v1/auth/signup`, `/login`, `/refresh`, `/logout`
- `GET /v1/auth/google/url`, `POST /v1/auth/google/exchange`
- `POST /v1/auth/password/recovery`, `PUT /v1/auth/password`
- `GET /v1/me`
- `GET /v1/account/subscriptions`, `POST /v1/account/checkout`
- `POST /v1/account/subscriptions/{id}/cancel`, `DELETE /v1/account`
- `POST /v1/account/stripe/webhook`
- `GET /v1/admin/users`, `PATCH /v1/admin/users/{id}/role`
- `GET /v1/admin/subscriptions`
- `GET /v1/admin/settings`, `PUT /v1/admin/settings/{key}`
- `GET /v1/admin/notifications`
- `POST /v1/admin/notifications`

Monthly Stripe billing is implemented but remains disabled until its server
environment variables are configured. Create a recurring BRL 5.00/month Stripe
Price for **Plano Pro**, then set `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID`,
`STRIPE_WEBHOOK_SECRET`, `STRIPE_SUCCESS_URL`, and `STRIPE_CANCEL_URL` in the
backend's encrypted environment. Never put Stripe secrets in the desktop app.
Configure a Stripe webhook pointing to `/v1/account/stripe/webhook` for
`customer.subscription.created`, `customer.subscription.updated`,
`customer.subscription.deleted`, and `checkout.session.expired`. The checkout
uses Stripe-hosted payment pages; the success/cancel URLs must be URLs that
Stripe accepts and that provide a suitable return destination for the app.

Users can cancel immediately or at the end of the paid period. Account deletion
is refused while a checkout or subscription is pending/active; once canceled
and no longer active, deleting the Supabase Auth user cascades related database
records and the backend removes the user's objects from the private image
bucket. Local conversation history remains on the device: it is not linked to
hosted accounts in the current version.

## Data access and integration

Every conversation, message, memory, image object, and subscription is tied to
the authenticated Supabase user. PostgreSQL Row Level Security is the actual
ownership boundary; filters in the UI are not treated as authorization. The
service-role credential bypasses RLS and is used only by the server's admin
operations.

Local SQLite history and the Flet interface have not been migrated or wired to
the hosted API. Plan a separate, explicit migration step to transfer local
history and images after accounts are available; do not delete the local
database as part of initial deployment.
