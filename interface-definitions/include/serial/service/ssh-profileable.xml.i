<!-- include start from serial/service/ssh-profileable.xml.i -->
<node name="ssh">
  <properties>
    <help>SSH service settings</help>
  </properties>
  <children>
    #include <include/serial/service/utils/idle-timeout.xml.i>
    #include <include/serial/service/utils/keepalive.xml.i>
    #include <include/serial/service/utils/modem-in-out.xml.i>
    #include <include/serial/service/utils/session-timeout.xml.i>
    #include <include/serial/service/utils/transmit-string-all.xml.i>
    <node name="client">
      <properties>
          <help>SSH client settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/initiate.xml.i>
        #include <include/serial/service/utils/remote.xml.i>
        #include <include/serial/service/utils/term-type.xml.i>
        <leafNode name="login-name">
          <properties>
            <help>Specifies the user to log in as on the remote machine</help>
            <constraint>
              <regex>.{0,21}</regex>
            </constraint>
            <constraintErrorMessage>Login username string too long (limit 21 characters)</constraintErrorMessage>
          </properties>
        </leafNode>
      </children>
    </node>
    <node name="server">
       <properties>
          <help>SSH server settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/multisession.xml.i>
        #include <include/serial/service/utils/listen-port.xml.i>
        #include <include/serial/service/utils/banner.xml.i>
        #include <include/serial/service/utils/motd.xml.i>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
