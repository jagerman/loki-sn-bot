--
-- PostgreSQL database dump
--

-- Dumped from database version 11.1 (Debian 11.1-1+b2)
-- Dumped by pg_dump version 11.1 (Debian 11.1-1+b2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_with_oids = false;

--
-- Name: service_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_nodes (
    id bigint NOT NULL,
    uid bigint NOT NULL,
    pubkey character(64) NOT NULL,
    active boolean DEFAULT false NOT NULL,
    complete boolean DEFAULT false NOT NULL,
    expires_soon boolean DEFAULT true NOT NULL,
    last_contributions bigint,
    last_reward_block_height bigint,
    alias text,
    note text,
    notified_dereg boolean DEFAULT false NOT NULL,
    notified_uptime_age integer,
    rewards boolean DEFAULT true NOT NULL,
    expiry_notified bigint,
    notified_age bigint,
    testnet boolean DEFAULT false NOT NULL,
    requested_unlock_height bigint,
    unlock_notified boolean DEFAULT false NOT NULL,
    notified_obsolete bigint,
    CONSTRAINT valid_sn_pubkey CHECK ((pubkey ~ similar_escape('[0-9a-f]{64}'::text, NULL::text)))
);


--
-- Name: service_nodes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.service_nodes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: service_nodes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.service_nodes_id_seq OWNED BY public.service_nodes.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id bigint NOT NULL,
    telegram_id bigint,
    discord_id text,
    faucet_last_used bigint,
    CONSTRAINT one_chat_id_required CHECK (((telegram_id IS NOT NULL) OR (discord_id IS NOT NULL)))
);


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: wallet_prefixes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wallet_prefixes (
    uid bigint NOT NULL,
    wallet text NOT NULL
);


--
-- Name: service_nodes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_nodes ALTER COLUMN id SET DEFAULT nextval('public.service_nodes_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: service_nodes service_nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_nodes
    ADD CONSTRAINT service_nodes_pkey PRIMARY KEY (id);


--
-- Name: service_nodes service_nodes_uid_pubkey_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_nodes
    ADD CONSTRAINT service_nodes_uid_pubkey_key UNIQUE (uid, pubkey);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: wallet_prefixes wallet_prefixes_uid_wallet_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_prefixes
    ADD CONSTRAINT wallet_prefixes_uid_wallet_key UNIQUE (uid, wallet);


--
-- Name: service_nodes_active_testnet_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX service_nodes_active_testnet_idx ON public.service_nodes USING btree (active, testnet);


--
-- Name: service_nodes_pubkey_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX service_nodes_pubkey_idx ON public.service_nodes USING btree (pubkey);


--
-- Name: users_discord_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX users_discord_id_idx ON public.users USING btree (discord_id);


--
-- Name: users_telegram_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX users_telegram_id_idx ON public.users USING btree (telegram_id);


--
-- Name: service_nodes service_nodes_uid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_nodes
    ADD CONSTRAINT service_nodes_uid_fkey FOREIGN KEY (uid) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: wallet_prefixes wallet_prefixes_uid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_prefixes
    ADD CONSTRAINT wallet_prefixes_uid_fkey FOREIGN KEY (uid) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

