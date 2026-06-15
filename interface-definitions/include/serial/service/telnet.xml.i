<!-- include start from serial/service/telnet.xml.i -->
<node name="telnet">
  <properties>
    <help>Telnet service settings</help>
  </properties>
  <children>
    #include <include/serial/service/utils/idle-timeout.xml.i>
    #include <include/serial/service/utils/keepalive.xml.i>
    #include <include/serial/service/utils/session-timeout.xml.i>
    #include <include/serial/service/utils/modem.xml.i>
    #include <include/serial/service/utils/transmit-string-all.xml.i>
    <node name="client">
      <properties>
          <help>Telnet client settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/initiate.xml.i>
        #include <include/serial/service/utils/remote.xml.i>
        #include <include/serial/service/utils/term-type.xml.i>
        <leafNode name="map-cr-to-crlf">
          <properties>
            <help>Enable mapping carriage returns (CR) to carriage return line feed (CRLF)</help>
            <valueless/>
          </properties>
        </leafNode>
      </children>
    </node>
    <node name="server">
       <properties>
          <help>Telnet server settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/auth-user.xml.i>
        #include <include/serial/service/utils/multisession.xml.i>
        #include <include/serial/service/utils/listen-port.xml.i>
        #include <include/serial/service/utils/banner.xml.i>
        #include <include/serial/service/utils/motd.xml.i>
        #include <include/serial/service/utils/aliasing-address.xml.i>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
